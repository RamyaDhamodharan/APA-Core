import uuid
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from src.models.document_models import Document
from src.models.document_workflow_models import DocumentWorkflow
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor
from src.schemas.document_workflow_schemas import (
    InvoiceRecommendation,
    WorkflowCandidate,
    WorkflowCheck,
    WorkflowDocument,
    WorkflowResult,
)
from src.services.invoice_confidence_services import (
    confidence_unavailable,
    requires_verification,
)
from src.services.invoice_number_services import normalize_invoice_no
from src.services.po_matching_services import normalize_value
from src.services.tolerance_config_services import (
    read_nonnegative_decimal,
    tolerance_fraction,
)
from src.services.vendor_resolution_services import resolve_vendor

DOCUMENT_TYPE_BY_MODEL = {
    Invoice: "invoice",
    PurchaseOrder: "po",
    GoodsReceipt: "goods_receipt",
}

MATCH_PRICE_TOLERANCE_PCT = tolerance_fraction("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2")
MATCH_TOTAL_TOLERANCE_PCT = tolerance_fraction("INVOICE_TOTAL_TOLERANCE_PCT", "1")


def _tax_tolerances() -> tuple[Decimal, Decimal]:
    absolute = read_nonnegative_decimal("TAX_TOLERANCE_ABS", "0.01")
    percent = tolerance_fraction("TAX_TOLERANCE_PERCENT", "0.5")
    return absolute, percent


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _owner_scope(model, owner_id):
    """Restrict a record to its creator or its owned source document."""
    if owner_id is None:
        return []
    expressions = []
    if hasattr(model, "created_by"):
        expressions.append(model.created_by == owner_id)
    if hasattr(model, "owner_user_id"):
        expressions.append(model.owner_user_id == owner_id)
    if hasattr(model, "source_document_id"):
        expressions.append(
            model.source_document_id.in_(
                select(Document.id).where(Document.created_by == owner_id)
            )
        )
    return [or_(*expressions)] if expressions else []


def _receipt_documents_for_reference(
    db: Session, po_number: object, owner_id=None
) -> list[Document]:
    normalized = normalize_value(po_number)
    if not normalized:
        return []
    reference_fields = ("order_number", "po_number", "po_number_raw")
    expressions = [
        func.lower(func.trim(Document.extracted_json[field].astext)) == normalized
        for field in reference_fields
    ]
    return (
        db.query(Document)
        .filter(
            Document.doc_type == "goods_receipt",
            or_(*expressions),
            *_owner_scope(Document, owner_id),
        )
        .all()
    )


def compare_available_records(
    *,
    db: Session | None = None,
    invoice: Invoice | None,
    purchase_order: PurchaseOrder | None,
    receipts: list[GoodsReceipt],
    po_lines: list[PurchaseOrderLine],
    invoice_lines: list[InvoiceLine],
) -> list[WorkflowCheck]:
    """Compare only evidence that is present; missing evidence is never a pass."""
    checks: list[WorkflowCheck] = []

    def check(
        name: str,
        outcome: str,
        reason: str,
        *,
        expected=None,
        actual=None,
        difference=None,
        severity: str | None = None,
    ) -> None:
        checks.append(
            WorkflowCheck(
                name=name,
                outcome=outcome,
                reason=reason,
                expected=expected,
                actual=actual,
                difference=difference,
                severity=severity
                or (
                    "error"
                    if outcome == "mismatch"
                    else "warning"
                    if outcome == "not_possible"
                    else "info"
                ),
            )
        )

    if invoice is not None and purchase_order is not None:
        invoice_vendor_id = getattr(invoice, "vendor_id", None)
        po_vendor_id = getattr(purchase_order, "vendor_id", None)
        invoice_vendor = (
            db.get(Vendor, invoice_vendor_id)
            if db is not None and invoice_vendor_id
            else None
        )
        po_vendor = (
            db.get(Vendor, po_vendor_id) if db is not None and po_vendor_id else None
        )
        invoice_tax_id = normalize_value(
            getattr(invoice, "vendor_tax_id_raw", None)
            or getattr(invoice_vendor, "tax_id", None)
        ).casefold()
        po_tax_id = normalize_value(getattr(po_vendor, "tax_id", None)).casefold()
        vendor_ok = bool(invoice_vendor_id and po_vendor_id)
        if not vendor_ok and not (invoice_tax_id and po_tax_id):
            check("vendor", "not_possible", "Invoice or PO vendor is unresolved")
        elif invoice_vendor_id == po_vendor_id or (
            invoice_tax_id and invoice_tax_id == po_tax_id
        ):
            check(
                "vendor", "passed", "Invoice and PO vendor ID or tax ID match exactly"
            )
        else:
            check(
                "vendor",
                "mismatch",
                "Invoice and PO resolve to different vendors",
                expected=str(po_vendor_id),
                actual=str(invoice_vendor_id),
            )

        invoice_ref = normalize_value(invoice.po_number_raw)
        po_ref = normalize_value(purchase_order.po_number)
        if not invoice_ref:
            check("po_reference", "not_possible", "Invoice PO reference is missing")
        elif invoice_ref == po_ref:
            check("po_reference", "passed", "Invoice PO reference matches")
        else:
            check(
                "po_reference",
                "mismatch",
                "Invoice PO reference differs from the selected PO",
                expected=purchase_order.po_number,
                actual=invoice.po_number_raw,
            )

        invoice_currency = normalize_value(invoice.currency).upper()
        po_currency = normalize_value(purchase_order.currency).upper()
        if not invoice_currency or not po_currency:
            check("currency", "not_possible", "Invoice or PO currency is missing")
        elif invoice_currency == po_currency:
            check("currency", "passed", "Invoice and PO currencies match")
        else:
            check(
                "currency",
                "mismatch",
                f"Currency mismatch: invoice {invoice_currency}, PO {po_currency}",
                expected=po_currency,
                actual=invoice_currency,
            )

        invoice_tax = _decimal(getattr(invoice, "tax_amount", None))
        expected_tax = _decimal(getattr(purchase_order, "tax_amount", None))
        if expected_tax is None:
            rate = _decimal(getattr(purchase_order, "tax_rate", None))
            subtotal = _decimal(getattr(purchase_order, "subtotal", None))
            if rate is None and db is not None and invoice.vendor_id:
                vendor = db.get(Vendor, invoice.vendor_id)
                rate = _decimal(getattr(vendor, "default_tax_rate", None))
                subtotal = _decimal(getattr(invoice, "subtotal", None))
            if rate is not None and subtotal is not None:
                expected_tax = subtotal * rate / Decimal(100)
        if expected_tax is None or invoice_tax is None:
            check(
                "tax",
                "not_possible",
                "Tax could not be verified",
                expected=expected_tax,
                actual=invoice_tax,
                severity="warning",
            )
        else:
            currency = (invoice.currency or "").strip().upper()
            exponent = (
                0
                if currency in {"JPY", "KRW", "CLP", "VND"}
                else 3
                if currency in {"BHD", "JOD", "KWD", "OMR", "TND"}
                else 2
            )
            quantum = Decimal(1).scaleb(-exponent)
            expected_tax = expected_tax.quantize(quantum)
            invoice_tax = invoice_tax.quantize(quantum)
            tax_tolerance_abs, tax_tolerance_percent = _tax_tolerances()
            tax_tolerance = max(
                tax_tolerance_abs,
                abs(expected_tax) * tax_tolerance_percent,
            )
            tax_difference = invoice_tax - expected_tax
            check(
                "tax",
                "passed" if abs(tax_difference) <= tax_tolerance else "mismatch",
                "Invoice tax compared with expected tax within tolerance"
                if abs(tax_difference) <= tax_tolerance
                else "Tax does not match expected",
                expected=expected_tax,
                actual=invoice_tax,
                difference=tax_difference,
            )

        invoice_total = _decimal(invoice.total_amount)
        po_total = _decimal(purchase_order.total_amount)
        if invoice_total is None or po_total is None:
            check(
                "document_total",
                "not_possible",
                "Invoice or PO total is missing or invalid",
            )
        elif (
            invoice_total - po_total
            > max(abs(po_total), Decimal("0.01")) * MATCH_TOTAL_TOLERANCE_PCT
        ):
            check(
                "document_total",
                "mismatch",
                "Invoice total exceeds the PO total",
                expected=po_total,
                actual=invoice_total,
                difference=invoice_total - po_total,
            )
    elif invoice is not None:
        check(
            "invoice_po_comparison",
            "not_possible",
            "Purchase order has not been identified",
        )
    elif purchase_order is not None:
        check(
            "po_invoice_comparison", "not_possible", "Invoice has not been identified"
        )

    if purchase_order is not None:
        if not receipts:
            check(
                "goods_receipt",
                "not_possible",
                "No goods receipt is available for comparison",
            )
        if receipts and any(receipt.po_id != purchase_order.id for receipt in receipts):
            check(
                "receipt_po_reference",
                "mismatch",
                "A receipt is associated with a different purchase order",
                expected=str(purchase_order.id),
                actual=", ".join(str(receipt.po_id) for receipt in receipts),
            )
        if receipts:
            receipt_vendors = {receipt.vendor_id for receipt in receipts}
            if not purchase_order.vendor_id or None in receipt_vendors:
                check(
                    "receipt_vendor",
                    "not_possible",
                    "A receipt or PO vendor is unresolved",
                )
            elif receipt_vendors == {purchase_order.vendor_id}:
                check("receipt_vendor", "passed", "Receipt vendors match the PO vendor")
            else:
                check(
                    "receipt_vendor",
                    "mismatch",
                    "At least one receipt vendor differs from the PO vendor",
                    expected=str(purchase_order.vendor_id),
                    actual=", ".join(sorted(str(value) for value in receipt_vendors)),
                )

        po_by_id = {line.id: line for line in po_lines}
        received: dict[object, Decimal] = {}
        accepted: dict[object, Decimal] = {}
        for receipt in receipts:
            for receipt_line in receipt.lines:
                po_line = po_by_id.get(receipt_line.po_line_id)
                if po_line is None:
                    check(
                        "receipt_po_line",
                        "mismatch",
                        "Receipt line references a line outside the selected PO",
                        actual=str(receipt_line.po_line_id),
                    )
                    continue
                po_uom = normalize_value(po_line.uom)
                receipt_uom = normalize_value(receipt_line.uom)
                if not po_uom or not receipt_uom:
                    check(
                        "unit_of_measure",
                        "not_possible",
                        "PO or receipt unit of measure is missing",
                        expected=po_line.uom,
                        actual=receipt_line.uom,
                    )
                    continue
                if po_uom != receipt_uom:
                    check(
                        "unit_of_measure",
                        "mismatch",
                        "PO and receipt units differ; quantities were not compared",
                        expected=po_line.uom,
                        actual=receipt_line.uom,
                    )
                    continue
                rec_qty = _decimal(receipt_line.received_qty)
                accepted_qty = _decimal(receipt_line.accepted_qty)
                rejected_qty = _decimal(receipt_line.rejected_qty)
                if rec_qty is None or accepted_qty is None or rejected_qty is None:
                    check(
                        "receipt_quantity",
                        "not_possible",
                        "Receipt quantity is missing or invalid",
                    )
                    continue
                if (
                    rec_qty <= 0
                    or accepted_qty < 0
                    or rejected_qty < 0
                    or accepted_qty + rejected_qty != rec_qty
                ):
                    check(
                        "receipt_quantity",
                        "mismatch",
                        "Receipt received, accepted, and rejected quantities are "
                        "inconsistent",
                        expected=rec_qty,
                        actual=accepted_qty + rejected_qty,
                    )
                    continue
                received[po_line.id] = received.get(po_line.id, Decimal(0)) + rec_qty
                accepted[po_line.id] = (
                    accepted.get(po_line.id, Decimal(0)) + accepted_qty
                )

        if not po_lines:
            check(
                "receipt_quantities",
                "not_possible",
                "PO line quantities are unavailable",
            )
        elif receipts:
            fully_received = True
            for po_line in po_lines:
                ordered = _decimal(po_line.qty)
                if ordered is None:
                    fully_received = False
                    check(
                        "receipt_quantity",
                        "not_possible",
                        f"Ordered quantity is unavailable for PO line {po_line.id}",
                    )
                    continue
                actual_received = received.get(po_line.id, Decimal(0))
                actual_accepted = accepted.get(po_line.id, Decimal(0))
                if actual_received > ordered:
                    fully_received = False
                    check(
                        "over_receipt",
                        "mismatch",
                        "Received quantity exceeds ordered quantity",
                        expected=ordered,
                        actual=actual_received,
                        difference=actual_received - ordered,
                    )
                elif actual_accepted < ordered:
                    fully_received = False
                    check(
                        "partial_receipt",
                        "passed",
                        "Accepted quantity is below the PO quantity; this is a "
                        "partial delivery",
                        expected=ordered,
                        actual=actual_accepted,
                        difference=actual_accepted - ordered,
                    )
            if invoice is not None and fully_received:
                invoice_total = _decimal(invoice.total_amount)
                po_total = _decimal(purchase_order.total_amount)
                if invoice_total is None or po_total is None:
                    check(
                        "document_total",
                        "not_possible",
                        "Invoice or fully received PO total is missing or invalid",
                    )
                else:
                    difference = invoice_total - po_total
                    if (
                        abs(difference)
                        <= max(abs(po_total), Decimal("0.01"))
                        * MATCH_TOTAL_TOLERANCE_PCT
                    ):
                        check(
                            "document_total",
                            "passed",
                            "Invoice total matches the fully received PO total",
                            expected=po_total,
                            actual=invoice_total,
                            difference=difference,
                        )
                    else:
                        check(
                            "document_total",
                            "mismatch",
                            "Invoice total differs from the fully received PO total",
                            expected=po_total,
                            actual=invoice_total,
                            difference=difference,
                        )

        if invoice is not None:
            invoice_by_po_line: dict[object, Decimal] = {}
            for inv_line in invoice_lines:
                po_line_id = inv_line.po_line_id
                if po_line_id is None:
                    matches = [
                        line
                        for line in po_lines
                        if normalize_value(line.item_code)
                        and normalize_value(line.item_code)
                        == normalize_value(inv_line.item_code)
                    ]
                    if not matches and not inv_line.item_code and inv_line.description:
                        matches = [
                            line
                            for line in po_lines
                            if normalize_value(line.description)
                            == normalize_value(inv_line.description)
                        ]
                    if len(matches) != 1:
                        check(
                            "invoice_line_link",
                            "not_possible",
                            "Invoice line cannot be uniquely mapped to a PO line",
                        )
                        continue
                    po_line_id = matches[0].id
                po_line = po_by_id.get(po_line_id)
                if po_line is None:
                    check(
                        "invoice_line_link",
                        "mismatch",
                        "Invoice line references a line outside the selected PO",
                        actual=str(po_line_id),
                    )
                    continue
                inv_uom = normalize_value(inv_line.uom)
                po_uom = normalize_value(po_line.uom)
                if not inv_uom or not po_uom:
                    check(
                        "unit_of_measure",
                        "not_possible",
                        "Invoice or PO unit of measure is missing",
                    )
                    continue
                if inv_uom != po_uom:
                    check(
                        "unit_of_measure",
                        "mismatch",
                        "Invoice and PO units differ; quantities were not compared",
                        expected=po_line.uom,
                        actual=inv_line.uom,
                    )
                    continue
                inv_qty = _decimal(inv_line.qty)
                ordered = _decimal(po_line.qty)
                if inv_qty is None or inv_qty <= 0 or ordered is None:
                    check(
                        "invoice_quantity",
                        "not_possible",
                        "Invoice or PO line quantity is missing or invalid",
                    )
                    continue
                invoice_by_po_line[po_line_id] = (
                    invoice_by_po_line.get(po_line_id, Decimal(0)) + inv_qty
                )
                if inv_qty > ordered:
                    check(
                        "invoice_vs_ordered_quantity",
                        "mismatch",
                        "Invoiced quantity exceeds the PO quantity",
                        expected=ordered,
                        actual=inv_qty,
                        difference=inv_qty - ordered,
                    )
                unit_price = _decimal(po_line.unit_price)
                invoice_unit_price = _decimal(inv_line.unit_price)
                line_amount = _decimal(inv_line.line_amount)
                if unit_price is None or invoice_unit_price is None:
                    check(
                        "unit_price",
                        "not_possible",
                        "Invoice or PO unit price is missing or invalid",
                    )
                elif invoice_unit_price == unit_price or (
                    unit_price != 0
                    and abs(invoice_unit_price - unit_price) / abs(unit_price)
                    <= MATCH_PRICE_TOLERANCE_PCT
                ):
                    check(
                        "unit_price",
                        "passed",
                        "Invoice unit price matches PO within "
                        f"{MATCH_PRICE_TOLERANCE_PCT * 100}%",
                        expected=unit_price,
                        actual=invoice_unit_price,
                        difference=invoice_unit_price - unit_price,
                    )
                else:
                    check(
                        "unit_price",
                        "mismatch",
                        "Invoice unit price differs from PO by more than "
                        f"{MATCH_PRICE_TOLERANCE_PCT * 100}%",
                        expected=unit_price,
                        actual=invoice_unit_price,
                        difference=invoice_unit_price - unit_price,
                    )
                if unit_price is None or line_amount is None:
                    check(
                        "line_price",
                        "not_possible",
                        "Invoice line amount or PO unit price is missing",
                    )
                else:
                    expected_amount = unit_price * inv_qty
                    difference = line_amount - expected_amount
                    if (
                        abs(difference)
                        <= max(abs(expected_amount), Decimal("0.01"))
                        * MATCH_TOTAL_TOLERANCE_PCT
                    ):
                        check(
                            "line_price",
                            "passed",
                            "Invoice line amount matches within "
                            f"{MATCH_TOTAL_TOLERANCE_PCT * 100}%",
                            expected=expected_amount,
                            actual=line_amount,
                            difference=difference,
                        )
                    else:
                        check(
                            "line_price",
                            "mismatch",
                            "Invoice line amount differs from PO unit price",
                            expected=expected_amount,
                            actual=line_amount,
                            difference=difference,
                        )
            if not invoice_lines:
                check(
                    "invoice_lines",
                    "not_possible",
                    "Invoice line items are unavailable",
                )
            else:
                if receipts:
                    for po_line_id, invoiced_qty in invoice_by_po_line.items():
                        accepted_qty = accepted.get(po_line_id, Decimal(0))
                        if invoiced_qty > accepted_qty:
                            check(
                                "invoice_vs_accepted_quantity",
                                "mismatch",
                                "Invoiced quantity exceeds accepted receipt quantity",
                                expected=accepted_qty,
                                actual=invoiced_qty,
                                difference=invoiced_qty - accepted_qty,
                            )
                else:
                    check(
                        "invoice_vs_accepted_quantity",
                        "mismatch",
                        "A goods receipt is missing; billed quantity cannot be "
                        "verified",
                        severity="error",
                    )
                invoice_total = _decimal(invoice.total_amount)
                line_total_values = [
                    _decimal(line.line_amount) for line in invoice_lines
                ]
                if invoice_total is None or any(
                    value is None for value in line_total_values
                ):
                    check(
                        "invoice_line_total",
                        "not_possible",
                        "Invoice total or a line amount is missing or invalid",
                    )
                else:
                    line_total = sum(line_total_values, Decimal(0))
                    difference = line_total - invoice_total
                    if (
                        abs(difference)
                        <= max(abs(invoice_total), Decimal("0.01"))
                        * MATCH_TOTAL_TOLERANCE_PCT
                    ):
                        check(
                            "invoice_line_total",
                            "passed",
                            "Invoice line amounts sum to the invoice total within "
                            f"{MATCH_TOTAL_TOLERANCE_PCT * 100}%",
                            expected=invoice_total,
                            actual=line_total,
                            difference=difference,
                        )
                    else:
                        check(
                            "invoice_line_total",
                            "mismatch",
                            "Invoice line amounts do not sum to the invoice total",
                            expected=invoice_total,
                            actual=line_total,
                            difference=difference,
                        )

    return checks


def evaluate_document_workflow(
    db: Session, document_id, *, owner_id=None
) -> WorkflowResult:
    """Build a deterministic current workflow view for any uploaded document."""
    document = db.get(Document, document_id)
    if document is None:
        raise LookupError("Document not found")
    workflow_state = (
        db.query(DocumentWorkflow)
        .filter(DocumentWorkflow.document_id == document.id)
        .first()
    )
    confirmed_invoice_id = (
        workflow_state.decision_target_id
        if workflow_state is not None
        and workflow_state.decision == "confirm_candidate"
        and workflow_state.decision_target_type == "invoice"
        else None
    )

    extraction = (
        document.extracted_json if isinstance(document.extracted_json, dict) else {}
    )
    po_number = (
        extraction.get("order_number")
        or extraction.get("po_number")
        or extraction.get("po_number_raw")
    )
    invoice = (
        db.query(Invoice).filter(Invoice.source_document_id == document.id).first()
    )
    purchase_order = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.source_document_id == document.id,
            *_owner_scope(PurchaseOrder, owner_id),
        )
        .first()
    )
    receipts = (
        db.query(GoodsReceipt)
        .options(selectinload(GoodsReceipt.lines))
        .filter(
            GoodsReceipt.source_document_id == document.id,
            *_owner_scope(GoodsReceipt, owner_id),
        )
        .all()
    )

    if invoice is not None and purchase_order is None and invoice.po_id:
        purchase_order = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.id == invoice.po_id,
                *_owner_scope(PurchaseOrder, owner_id),
            )
            .first()
        )
    if receipts and purchase_order is None:
        purchase_order = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.id == receipts[0].po_id,
                *_owner_scope(PurchaseOrder, owner_id),
            )
            .first()
        )
    if confirmed_invoice_id is not None:
        selected_invoice = (
            db.query(Invoice)
            .filter(
                Invoice.id == confirmed_invoice_id,
                *_owner_scope(Invoice, owner_id),
            )
            .first()
        )
        if selected_invoice is not None and selected_invoice.is_current:
            invoice = selected_invoice
            if purchase_order is None and selected_invoice.po_id:
                purchase_order = (
                    db.query(PurchaseOrder)
                    .filter(
                        PurchaseOrder.id == selected_invoice.po_id,
                        *_owner_scope(PurchaseOrder, owner_id),
                    )
                    .first()
                )
    if purchase_order is None and po_number:
        candidates = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.is_current.is_(True),
                *_owner_scope(PurchaseOrder, owner_id),
            )
            .all()
        )
        candidates = [
            po
            for po in candidates
            if normalize_value(po.po_number) == normalize_value(po_number)
        ]
    else:
        candidates = [purchase_order] if purchase_order is not None else []

    ambiguous = len(candidates) > 1
    if purchase_order is None and len(candidates) == 1:
        purchase_order = candidates[0]
    invoice_candidates: list[Invoice] = []
    if invoice is None and purchase_order is None and po_number:
        invoice_candidates = (
            db.query(Invoice)
            .filter(Invoice.is_current.is_(True), *_owner_scope(Invoice, owner_id))
            .all()
        )
        invoice_candidates = [
            row
            for row in invoice_candidates
            if normalize_value(row.po_number_raw) == normalize_value(po_number)
        ]
        invoice = invoice_candidates[0] if len(invoice_candidates) == 1 else None
        ambiguous = ambiguous or len(invoice_candidates) > 1
    if invoice is None and purchase_order is not None:
        invoice_candidates = (
            db.query(Invoice)
            .filter(Invoice.is_current.is_(True), *_owner_scope(Invoice, owner_id))
            .all()
        )
        invoice_candidates = [
            row
            for row in invoice_candidates
            if row.po_id == purchase_order.id
            or normalize_value(row.po_number_raw)
            == normalize_value(purchase_order.po_number)
        ]
        invoice = invoice_candidates[0] if len(invoice_candidates) == 1 else None
        ambiguous = ambiguous or len(invoice_candidates) > 1
    if purchase_order is not None:
        receipts = (
            db.query(GoodsReceipt)
            .options(selectinload(GoodsReceipt.lines))
            .filter(
                GoodsReceipt.po_id == purchase_order.id,
                *_owner_scope(GoodsReceipt, owner_id),
            )
            .all()
        )

    related_receipt_documents = _receipt_documents_for_reference(
        db,
        purchase_order.po_number if purchase_order is not None else po_number,
        owner_id,
    )

    po_lines = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == purchase_order.id)
        .all()
        if purchase_order is not None
        else []
    )
    invoice_lines = (
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
        if invoice is not None
        else []
    )
    duplicate_invoices: list[Invoice] = []
    if invoice is not None and invoice.vendor_id and invoice.invoice_no:
        normalized_no = getattr(invoice, "invoice_no_normalized", None) or (
            normalize_invoice_no(invoice.invoice_no)
        )
        duplicate_invoices = (
            db.query(Invoice)
            .filter(
                Invoice.vendor_id == invoice.vendor_id,
                Invoice.invoice_no_normalized == normalized_no,
                Invoice.is_current.is_(True),
                Invoice.id != invoice.id,
                *_owner_scope(Invoice, owner_id),
            )
            .all()
        )

    seen_receipt_numbers: dict[str, GoodsReceipt] = {}
    duplicate_receipt_pairs: list[tuple[GoodsReceipt, GoodsReceipt]] = []
    for receipt in receipts:
        normalized_number = normalize_value(receipt.gr_number)
        if not normalized_number:
            continue
        previous = seen_receipt_numbers.get(normalized_number)
        if previous is not None:
            duplicate_receipt_pairs.append((previous, receipt))
        else:
            seen_receipt_numbers[normalized_number] = receipt

    docs: dict[str, WorkflowDocument] = {
        str(document.id): WorkflowDocument(
            id=document.id,
            type=document.doc_type or "unknown",
            reference=str(po_number) if po_number else None,
        )
    }
    for record in (invoice, purchase_order, *receipts):
        if record is None:
            continue
        source_id = getattr(record, "source_document_id", None)
        if source_id:
            source = (
                db.query(Document)
                .filter(Document.id == source_id, *_owner_scope(Document, owner_id))
                .first()
            )
            if source is not None:
                docs[str(source.id)] = WorkflowDocument(
                    id=source.id,
                    type=source.doc_type or "unknown",
                    reference=(
                        getattr(record, "invoice_no", None)
                        or getattr(record, "po_number", None)
                        or getattr(record, "gr_number", None)
                    ),
                )

    if invoice is not None:
        docs[f"invoice:{invoice.id}"] = WorkflowDocument(
            id=invoice.id, type="invoice", reference=invoice.invoice_no
        )
    if purchase_order is not None:
        docs[f"po:{purchase_order.id}"] = WorkflowDocument(
            id=purchase_order.id, type="po", reference=purchase_order.po_number
        )
    for receipt in receipts:
        docs[f"gr:{receipt.id}"] = WorkflowDocument(
            id=receipt.id, type="goods_receipt", reference=receipt.gr_number
        )
    for related_doc in related_receipt_documents:
        docs[str(related_doc.id)] = WorkflowDocument(
            id=related_doc.id,
            type=related_doc.doc_type or "goods_receipt",
            reference=str(
                related_doc.extracted_json.get("gr_number")
                or related_doc.extracted_json.get("receipt_number")
                or ""
            )
            or None,
        )

    types_present = {
        item.type
        for item in docs.values()
        if item.type in {"invoice", "po", "goods_receipt"}
    }
    missing = [
        kind for kind in ("po", "goods_receipt", "invoice") if kind not in types_present
    ]
    checks = compare_available_records(
        db=db,
        invoice=invoice,
        purchase_order=purchase_order,
        receipts=receipts,
        po_lines=po_lines,
        invoice_lines=invoice_lines,
    )
    for previous, receipt in duplicate_receipt_pairs:
        checks.append(
            WorkflowCheck(
                name="duplicate_goods_receipt",
                outcome="mismatch",
                severity="error",
                expected=str(previous.id),
                actual=str(receipt.id),
                reason="Multiple stored goods receipts share the same GR number",
            )
        )
    for duplicate_invoice in duplicate_invoices:
        checks.append(
            WorkflowCheck(
                name="duplicate_invoice",
                outcome="mismatch",
                severity="error",
                expected=str(invoice.id),
                actual=str(duplicate_invoice.id),
                reason="Another current invoice has the same vendor and invoice number",
            )
        )
    if invoice is not None and purchase_order is None and related_receipt_documents:
        accepted_by_key: dict[tuple[str, str], Decimal] = {}
        receipt_units: dict[tuple[str, str], str] = {}
        for receipt_doc in related_receipt_documents:
            receipt_data = receipt_doc.extracted_json or {}
            receipt_vendor = normalize_value(receipt_data.get("vendor"))
            invoice_vendor = normalize_value(invoice.vendor_name_raw)
            if not receipt_vendor or not invoice_vendor:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_vendor",
                        outcome="not_possible",
                        severity="warning",
                        reason="Invoice or receipt vendor identity is missing",
                    )
                )
            else:
                resolved_receipt_vendor = resolve_vendor(db, receipt_data.get("vendor"))
                resolved_invoice_vendor = resolve_vendor(db, invoice.vendor_name_raw)
                if (
                    resolved_receipt_vendor is not None
                    and resolved_invoice_vendor is not None
                    and resolved_receipt_vendor.id != resolved_invoice_vendor.id
                ):
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_vendor",
                            outcome="mismatch",
                            severity="error",
                            expected=str(resolved_invoice_vendor.id),
                            actual=str(resolved_receipt_vendor.id),
                            reason=(
                                "Invoice and extracted receipt resolve to "
                                "different vendors"
                            ),
                        )
                    )
                elif (
                    resolved_receipt_vendor is not None
                    and resolved_invoice_vendor is not None
                    and resolved_receipt_vendor.id == resolved_invoice_vendor.id
                ):
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_vendor",
                            outcome="passed",
                            reason=(
                                "Invoice and extracted receipt resolve to the "
                                "same vendor"
                            ),
                        )
                    )
                elif receipt_vendor == invoice_vendor:
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_vendor",
                            outcome="passed",
                            reason="Invoice and extracted receipt vendor names match",
                        )
                    )
                else:
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_vendor",
                            outcome="not_possible",
                            severity="warning",
                            reason=(
                                "Different vendor names could not be resolved "
                                "to compare identity"
                            ),
                        )
                    )
            gr_ref = normalize_value(
                receipt_data.get("order_number") or receipt_data.get("po_number")
            )
            inv_ref = normalize_value(invoice.po_number_raw)
            if not gr_ref or not inv_ref:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_po_reference",
                        outcome="not_possible",
                        severity="warning",
                        reason="Invoice or receipt PO reference is missing",
                    )
                )
            elif gr_ref == inv_ref:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_po_reference",
                        outcome="passed",
                        reason="Invoice and extracted receipt PO references match",
                    )
                )
            else:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_po_reference",
                        outcome="mismatch",
                        severity="error",
                        expected=invoice.po_number_raw,
                        actual=gr_ref,
                        reason="Invoice and extracted receipt PO references differ",
                    )
                )
            receipt_lines = receipt_data.get("line_items")
            if not isinstance(receipt_lines, list):
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_lines",
                        outcome="not_possible",
                        severity="warning",
                        reason="Extracted receipt line items are unavailable",
                    )
                )
                continue
            for line in receipt_lines:
                if not isinstance(line, dict):
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_lines",
                            outcome="not_possible",
                            severity="warning",
                            reason="An extracted receipt line is not an object",
                        )
                    )
                    continue
                item_code = normalize_value(
                    line.get("item_code") or line.get("sku") or line.get("product_code")
                )
                description = normalize_value(
                    line.get("description") or line.get("item_description")
                )
                key = (
                    ("item_code", item_code)
                    if item_code
                    else ("description", description)
                )
                received = _decimal(
                    line.get("received_qty") or line.get("quantity") or line.get("qty")
                )
                accepted_qty = _decimal(line.get("accepted_qty"))
                rejected_qty = _decimal(line.get("rejected_qty"))
                receipt_uom = normalize_value(
                    line.get("uom") or line.get("unit_of_measure")
                )
                if (
                    not key[1]
                    or not receipt_uom
                    or received is None
                    or accepted_qty is None
                    or rejected_qty is None
                ):
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_lines",
                            outcome="not_possible",
                            severity="warning",
                            reason=(
                                "Receipt line identity, unit, or quantities are missing"
                            ),
                        )
                    )
                    continue
                if (
                    received <= 0
                    or accepted_qty < 0
                    or rejected_qty < 0
                    or accepted_qty + rejected_qty != received
                ):
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_lines",
                            outcome="mismatch",
                            severity="error",
                            reason="Extracted receipt quantities are inconsistent",
                        )
                    )
                    continue
                existing_unit = receipt_units.get(key)
                if existing_unit is not None and existing_unit != receipt_uom:
                    checks.append(
                        WorkflowCheck(
                            name="invoice_receipt_units",
                            outcome="not_possible",
                            severity="warning",
                            expected=existing_unit,
                            actual=receipt_uom,
                            reason=(
                                "Receipt deliveries use incompatible units for "
                                "the same item"
                            ),
                        )
                    )
                    continue
                receipt_units[key] = receipt_uom
                accepted_by_key[key] = (
                    accepted_by_key.get(key, Decimal(0)) + accepted_qty
                )
        if not invoice_lines:
            checks.append(
                WorkflowCheck(
                    name="invoice_receipt_lines",
                    outcome="not_possible",
                    severity="warning",
                    reason="Invoice line items are unavailable",
                )
            )
        invoice_qty_by_key: dict[tuple[str, str], Decimal] = {}
        for line in invoice_lines:
            item_code = normalize_value(line.item_code)
            description = normalize_value(line.description)
            key = (
                ("item_code", item_code) if item_code else ("description", description)
            )
            quantity = _decimal(line.qty)
            invoice_uom = normalize_value(line.uom)
            if not key[1] or quantity is None or not invoice_uom:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_lines",
                        outcome="not_possible",
                        severity="warning",
                        reason="Invoice line identity, unit, or quantity is missing",
                    )
                )
                continue
            receipt_uom = receipt_units.get(key)
            if receipt_uom is None:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_lines",
                        outcome="not_possible",
                        severity="warning",
                        reason=(
                            "Invoice line could not be associated with a receipt line"
                        ),
                    )
                )
            elif receipt_uom != invoice_uom:
                checks.append(
                    WorkflowCheck(
                        name="invoice_receipt_units",
                        outcome="mismatch",
                        severity="error",
                        expected=receipt_uom,
                        actual=invoice_uom,
                        reason="Invoice and receipt line units differ",
                    )
                )
            else:
                invoice_qty_by_key[key] = (
                    invoice_qty_by_key.get(key, Decimal(0)) + quantity
                )
        for key, invoice_qty in invoice_qty_by_key.items():
            accepted_qty = accepted_by_key.get(key)
            if accepted_qty is None:
                continue
            outcome = "passed" if invoice_qty <= accepted_qty else "mismatch"
            checks.append(
                WorkflowCheck(
                    name="invoice_vs_accepted_quantity",
                    outcome=outcome,
                    severity="error" if outcome == "mismatch" else "info",
                    expected=accepted_qty,
                    actual=invoice_qty,
                    difference=invoice_qty - accepted_qty,
                    reason=(
                        "Invoiced quantity does not exceed accepted receipt quantity"
                        if outcome == "passed"
                        else "Invoiced quantity exceeds accepted receipt quantity"
                    ),
                )
            )
    if ambiguous:
        status = "awaiting_human_decision"
        checks.append(
            WorkflowCheck(
                name="candidate_resolution",
                outcome="not_possible",
                severity="warning",
                reason="Multiple related records match the available identifiers",
            )
        )
    elif document.status in {"received", "parsing", "parsed", "classified"}:
        status = "processing"
    elif document.status == "failed":
        status = "extraction_failed"
    elif document.status in {
        "needs_vendor_review",
        "needs_data_review",
        "incomplete",
    } or any(
        item.outcome == "mismatch" for item in checks
    ):
        status = "review_required"
    elif (
        invoice is not None
        and purchase_order is not None
        and bool(receipts)
        and len(types_present) >= 3
        and not missing
    ):
        if not checks or any(item.outcome != "passed" for item in checks):
            status = "review_required"
        elif any(item.name == "partial_receipt" for item in checks):
            status = "partial_match"
        else:
            status = "matched"
    elif len(types_present) >= 2:
        status = "partial_match"
    else:
        status = "awaiting_related_documents"

    candidates = [
        WorkflowCandidate(
            record_type="purchase_order",
            id=po.id,
            evidence={
                "po_number": po.po_number,
                "vendor_id": str(po.vendor_id) if po.vendor_id else None,
                "source_document_id": str(po.source_document_id)
                if po.source_document_id
                else None,
            },
        )
        for po in candidates
    ]
    candidates.extend(
        WorkflowCandidate(
            record_type="goods_receipt",
            id=receipt.id,
            evidence={
                "gr_number": receipt.gr_number,
                "po_id": str(receipt.po_id),
                "duplicate_of": str(previous.id),
            },
        )
        for previous, receipt in duplicate_receipt_pairs
    )
    if len(invoice_candidates) > 1 and invoice is None:
        candidates.extend(
            WorkflowCandidate(
                record_type="invoice",
                id=row.id,
                evidence={
                    "invoice_no": row.invoice_no,
                    "po_number": row.po_number_raw,
                    "vendor_id": str(row.vendor_id) if row.vendor_id else None,
                    "source_document_id": (
                        str(row.source_document_id) if row.source_document_id else None
                    ),
                },
            )
            for row in invoice_candidates
        )
    candidates.extend(
        WorkflowCandidate(
            record_type="invoice",
            id=row.id,
            evidence={
                "invoice_no": row.invoice_no,
                "vendor_id": str(row.vendor_id) if row.vendor_id else None,
                "source_document_id": (
                    str(row.source_document_id) if row.source_document_id else None
                ),
                "duplicate_of": str(invoice.id) if invoice is not None else None,
            },
        )
        for row in duplicate_invoices
    )
    actions = []
    if "po" in missing:
        actions.append("Upload or select the related purchase order")
    if "goods_receipt" in missing:
        actions.append("Upload or record the related goods receipt")
    if "invoice" in missing:
        actions.append("Upload the supplier invoice")
    if ambiguous:
        actions.append("Confirm the correct related record")
    if status in {"review_required", "partial_match"}:
        actions.append("Review the evidence and resolve listed differences")
    if status == "matched":
        actions.append(
            "Submit the invoice through the existing human approval workflow"
        )
    if document.status in {
        "failed",
        "needs_vendor_review",
        "needs_data_review",
        "incomplete",
    }:
        actions.append("Correct the extraction or vendor data, then retry processing")

    return WorkflowResult(
        document_id=document.id,
        status=status,
        available_documents=list(docs.values()),
        missing_document_types=missing,
        candidates=candidates,
        checks=checks,
        recommended_next_actions=actions,
        human_review_required=status
        not in {"processing", "awaiting_related_documents", "matched"},
        human_decision_required=ambiguous
        or status in {"review_required", "partial_match"},
    )


def _reason_code(check: WorkflowCheck) -> str:
    known = {
        "vendor": "vendor_conflict",
        "receipt_vendor": "vendor_conflict",
        "invoice_receipt_vendor": "vendor_conflict",
        "po_reference": "po_reference_conflict",
        "invoice_receipt_po_reference": "po_reference_conflict",
        "currency": "currency_conflict",
        "document_total": "total_discrepancy",
        "invoice_line_total": "line_total_discrepancy",
        "unit_price": "unit_price_discrepancy",
        "line_price": "line_price_discrepancy",
        "invoice_quantity": "quantity_discrepancy",
        "invoice_vs_ordered_quantity": "quantity_discrepancy",
        "invoice_vs_accepted_quantity": "quantity_discrepancy",
        "partial_receipt": "partial_receipt",
        "over_receipt": "over_receipt",
        "duplicate_invoice": "duplicate_invoice_candidate",
        "duplicate_goods_receipt": "duplicate_goods_receipt_candidate",
        "candidate_resolution": "ambiguous_candidates",
        "extraction_validation": "extraction_validation_issue",
        "extraction_failure": "extraction_failed",
    }
    return known.get(
        check.name,
        f"{check.outcome}_{check.name}".replace("-", "_").lower(),
    )


def _finding_category(check: WorkflowCheck) -> str:
    name = check.name
    if name.startswith("duplicate"):
        return "duplicate_integrity"
    if "vendor" in name:
        return "vendor_identity"
    if any(part in name for part in ("quantity", "receipt", "lines")):
        return "quantity_receipt"
    if any(part in name for part in ("total", "price", "currency", "tax")):
        return "financial"
    if "extraction" in name:
        return "extraction_validation"
    if "candidate" in name or "purchase_order" in name or "po_" in name:
        return "document_matching"
    return "evidence_availability"


def _annotate_findings(
    invoice_id: uuid.UUID,
    checks: list[WorkflowCheck],
    records: list[WorkflowDocument],
) -> list[WorkflowCheck]:
    evidence_ids = [record.id for record in records]
    annotated = []
    for check in checks:
        payload = "|".join(
            (
                str(invoice_id),
                check.name,
                str(check.expected),
                str(check.actual),
                str(check.difference),
                check.reason,
            )
        )
        finding_id = str(uuid.uuid5(uuid.NAMESPACE_URL, payload))
        finding_status = {
            "passed": "supported",
            "mismatch": "open",
            "not_possible": "not_verifiable",
        }[check.outcome]
        annotated.append(
            check.model_copy(
                update={
                    "finding_id": finding_id,
                    "category": _finding_category(check),
                    "finding_status": finding_status,
                    "evidence_record_ids": evidence_ids,
                }
            )
        )
    return annotated


def recommend_invoice_review(
    db: Session, invoice: Invoice, *, owner_id
) -> InvoiceRecommendation:
    """Return a deterministic recommendation using only uploader-owned records."""
    if invoice.source_document_id is not None:
        source = (
            db.query(Document)
            .filter(
                Document.id == invoice.source_document_id,
                or_(
                    Document.created_by == owner_id,
                    Document.id.in_(
                        select(Invoice.source_document_id).where(
                            or_(
                                Invoice.owner_user_id == owner_id,
                                Invoice.created_by == owner_id,
                            )
                        )
                    ),
                ),
            )
            .first()
        )
    else:
        source = None

    if source is not None:
        workflow = evaluate_document_workflow(db, source.id, owner_id=owner_id)
        checks = list(workflow.checks)
        confidence = getattr(invoice, "field_confidence", None) or {}
        if requires_verification(confidence):
            missing = confidence_unavailable(confidence)
            checks.append(
                WorkflowCheck(
                    name="confidence",
                    reason_code="confidence_unavailable"
                    if missing
                    else "low_confidence",
                    outcome="not_possible" if missing else "mismatch",
                    severity="warning",
                    reason=(
                        "Confidence unavailable, please verify"
                        if missing
                        else "One or more extracted fields need verification"
                    ),
                )
            )
        if source.status in {
            "needs_vendor_review",
            "needs_data_review",
            "incomplete",
            "failed",
        }:
            extraction_failed = source.status == "failed"
            checks.append(
                WorkflowCheck(
                    name=(
                        "extraction_failure"
                        if extraction_failed
                        else "extraction_validation"
                    ),
                    reason_code=(
                        "extraction_failed"
                        if extraction_failed
                        else "extraction_validation_issue"
                    ),
                    outcome="not_possible" if extraction_failed else "mismatch",
                    severity="warning" if extraction_failed else "error",
                    actual=source.failure_message,
                    reason=(source.failure_message or "Extraction needs review"),
                )
            )
        status = (
            "review_required" if requires_verification(confidence) else workflow.status
        )
        review_required = (
            requires_verification(confidence)
            or workflow.human_review_required
            or bool(
                source.status
                in {
                    "needs_vendor_review",
                    "needs_data_review",
                    "incomplete",
                    "failed",
                }
            )
        )
        action = (
            "Verify extracted invoice fields"
            if requires_verification(confidence)
            else workflow.recommended_next_actions[0]
            if workflow.recommended_next_actions
            else "Submit the invoice through the existing human approval workflow"
        )
        supporting = workflow.available_documents
        missing = workflow.missing_document_types
        candidates = workflow.candidates
        processing_status = source.status
    else:
        # Legacy/manual invoices without a source document remain reviewable, but
        # extraction and comparisons requiring absent evidence are explicitly unknown.
        po_candidates = []
        if invoice.po_id:
            po = (
                db.query(PurchaseOrder)
                .filter(
                    PurchaseOrder.id == invoice.po_id,
                    *_owner_scope(PurchaseOrder, owner_id),
                )
                .first()
            )
            if po is not None:
                po_candidates = [po]
        elif invoice.po_number_raw:
            po_candidates = [
                row
                for row in db.query(PurchaseOrder)
                .filter(
                    PurchaseOrder.is_current.is_(True),
                    *_owner_scope(PurchaseOrder, owner_id),
                )
                .all()
                if normalize_value(row.po_number)
                == normalize_value(invoice.po_number_raw)
            ]
        po = po_candidates[0] if len(po_candidates) == 1 else None
        receipts = (
            db.query(GoodsReceipt)
            .options(selectinload(GoodsReceipt.lines))
            .filter(
                GoodsReceipt.po_id == po.id,
                *_owner_scope(GoodsReceipt, owner_id),
            )
            .all()
            if po is not None
            else []
        )
        checks = []
        if po is not None:
            checks.extend(
                compare_available_records(
                    db=db,
                    invoice=invoice,
                    purchase_order=po,
                    receipts=receipts,
                    po_lines=db.query(PurchaseOrderLine)
                    .filter(PurchaseOrderLine.po_id == po.id)
                    .all(),
                    invoice_lines=db.query(InvoiceLine)
                    .filter(InvoiceLine.invoice_id == invoice.id)
                    .all(),
                )
            )
        else:
            checks.append(
                WorkflowCheck(
                    name="purchase_order",
                    outcome="not_possible",
                    reason="No uploader-owned purchase order could be resolved",
                )
            )
        supporting = [
            WorkflowDocument(
                id=invoice.id, type="invoice", reference=invoice.invoice_no
            )
        ]
        if po is not None:
            supporting.append(
                WorkflowDocument(id=po.id, type="po", reference=po.po_number)
            )
        supporting.extend(
            WorkflowDocument(
                id=receipt.id, type="goods_receipt", reference=receipt.gr_number
            )
            for receipt in receipts
        )
        missing = []
        if po is None:
            missing.append("po")
        if not receipts:
            missing.append("goods_receipt")
        candidates = [
            WorkflowCandidate(
                record_type="purchase_order",
                id=row.id,
                evidence={"po_number": row.po_number},
            )
            for row in po_candidates
        ]
        if len(po_candidates) > 1:
            checks.append(
                WorkflowCheck(
                    name="candidate_resolution",
                    reason_code="ambiguous_candidates",
                    outcome="not_possible",
                    severity="warning",
                    reason=(
                        "Multiple uploader-owned purchase orders match the reference"
                    ),
                )
            )
        has_mismatch = any(check.outcome == "mismatch" for check in checks)
        has_unknown = any(check.outcome == "not_possible" for check in checks)
        partial_receipt = any(
            check.name == "partial_receipt" and check.outcome == "passed"
            for check in checks
        )
        status = (
            "awaiting_human_decision"
            if len(po_candidates) > 1
            else (
                "review_required"
                if has_mismatch
                else "awaiting_related_documents"
                if missing
                else "partial_match"
                if partial_receipt
                else "review_required"
                if has_unknown
                else "matched"
            )
        )
        action = (
            "Select the correct purchase order"
            if len(po_candidates) > 1
            else "Upload or record the related purchase order"
            if po is None
            else "Upload or record the related goods receipt"
            if not receipts
            else "Review the evidence and resolve listed differences"
            if has_mismatch or has_unknown
            else "Review the partial delivery against the invoice"
            if partial_receipt
            else "Submit the invoice through the existing human approval workflow"
        )
        processing_status = "unavailable"
        review_required = status != "matched"

    checks = [
        check.model_copy(
            update={"reason_code": check.reason_code or _reason_code(check)}
        )
        for check in checks
    ]
    checks = _annotate_findings(invoice.id, checks, supporting)
    codes = list(
        dict.fromkeys(check.reason_code for check in checks if check.reason_code)
    )
    return InvoiceRecommendation(
        invoice_id=invoice.id,
        processing_status=processing_status,
        matching_status=status,
        review_status="required" if review_required else "ready_for_human_review",
        approval_status=invoice.final_status or "awaiting_decision",
        payment_status="unknown",
        supporting_records=supporting,
        missing_document_types=missing,
        candidates=candidates,
        checks=checks,
        discrepancies=[check for check in checks if check.outcome == "mismatch"],
        reason_codes=codes,
        next_recommended_action=action,
        human_review_required=review_required,
    )


def refresh_related_workflows(db: Session, document_id) -> list[WorkflowResult]:
    """Refresh this document and any currently discoverable related source docs."""
    anchor = evaluate_document_workflow(db, document_id)
    document_ids = {anchor.document_id}
    for available in anchor.available_documents:
        if db.get(Document, available.id) is not None:
            document_ids.add(available.id)

    results = []
    for source_id in document_ids:
        result = evaluate_document_workflow(db, source_id)
        state = (
            db.query(DocumentWorkflow)
            .filter(DocumentWorkflow.document_id == source_id)
            .with_for_update()
            .first()
        )
        if state is None:
            state = DocumentWorkflow(
                document_id=source_id, status=result.status, result={}
            )
            db.add(state)
        if state.decision == "hold":
            result = result.model_copy(
                update={"status": "on_hold", "human_review_required": True}
            )
        elif state.decision == "reject_candidate":
            result = result.model_copy(
                update={
                    "status": "awaiting_human_decision",
                    "human_decision_required": True,
                }
            )
        result = result.model_copy(
            update={
                "decision": state.decision,
                "decision_reason": state.decision_reason,
                "decision_target_id": state.decision_target_id,
                "decision_target_type": state.decision_target_type,
            }
        )
        state.status = result.status
        state.result = result.model_dump(mode="json")
        results.append(result)
    return results
