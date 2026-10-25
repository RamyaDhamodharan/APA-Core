"""
Application module for PO matching services.
Flow: normalize extracted tokens/amounts, perform 2-way match on document POs, and advisory 3-way match against database POs and Goods Receipts.
Called by: document_workflow_services, ap_decision_engine, invoice_routers.
Calls: invoice_number_services, models.
Tables: documents, invoices, invoice_lines, purchase_orders, po_lines, goods_receipts, gr_lines.
"""
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session, selectinload

from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.invoice_number_services import normalize_invoice_no

# Precompiled regex patterns for high-throughput string cleaning and amount parsing
_MULTI_SPACE = re.compile(r"\s+")
_CURRENCY_SYMBOLS = re.compile(r"[,$\s]")


def normalize_value(value: object) -> str:
    """
    Normalize text tokens for case-insensitive and whitespace-invariant comparison.
    Inputs: value (object) - raw string, number, or identifier.
    Output: str - normalized lowercase string with single spaces.
    Effects: pure function, no database operations.
    Results: uniform token for SKU, PO numbers, and descriptions.
    """
    if value is None:
        return ""
    # Collapse multiple spaces and convert to lowercase
    return _MULTI_SPACE.sub(" ", str(value)).strip().casefold()


def normalize_amount(value: object) -> Decimal | None:
    """
    Convert a monetary value string or number to a Decimal safely.
    Inputs: value (object) - currency string (e.g. '$1,234.50'), float, or int.
    Output: Decimal | None - finite Decimal amount or None if unparseable.
    Effects: pure function, no database operations.
    Results: exact decimal representation without binary floating-point rounding errors.
    """
    if value is None or isinstance(value, bool):
        return None

    try:
        # Strip currency signs, commas, and whitespace
        cleaned = _CURRENCY_SYMBOLS.sub("", str(value))
        amount = Decimal(cleaned)

        if not amount.is_finite():
            return None

        return amount
    except (InvalidOperation, ValueError):
        return None


def match_invoice_to_po(
    db: Session,
    invoice_data: dict,
    invoice_document_id,
) -> dict:
    """
    Match an invoice against an extracted purchase order document in storage.
    Inputs: db (Session), invoice_data (dict), invoice_document_id (UUID | str).
    Output: dict with keys 'matched' (bool), 'reason' (str), 'po_document_id' (str | None).
    Effects: reads database rows from 'documents' table.
    Results: initial document-level PO matching result.
    """
    # Step 1: Normalize incoming invoice fields
    vendor = normalize_value(invoice_data.get("vendor"))
    order_number = normalize_value(invoice_data.get("order_number"))
    invoice_total = normalize_amount(invoice_data.get("total"))

    # Step 2: Validate mandatory match keys
    if not vendor:
        return {
            "matched": False,
            "reason": "Invoice vendor is missing",
            "po_document_id": None,
        }

    if not order_number:
        return {
            "matched": False,
            "reason": "Invoice PO/order number is missing",
            "po_document_id": None,
        }

    if invoice_total is None:
        return {
            "matched": False,
            "reason": "Invoice total is missing or invalid",
            "po_document_id": None,
        }

    # Step 3: Query extracted PO documents in storage
    purchase_orders = (
        db.query(Document)
        .filter(
            Document.doc_type == "po",
            Document.extracted_json.isnot(None),
        )
        .all()
    )

    # Step 4: Search for exact matching PO
    for po in purchase_orders:
        if po.id == invoice_document_id:
            continue

        po_data = po.extracted_json or {}

        if not isinstance(po_data, dict):
            continue

        po_vendor = normalize_value(po_data.get("vendor"))
        po_number = normalize_value(po_data.get("order_number"))
        po_total = normalize_amount(po_data.get("total"))

        # Skip POs with different order numbers
        if po_number != order_number:
            continue

        # Check vendor alignment
        if po_vendor != vendor:
            return {
                "matched": False,
                "reason": "PO vendor does not match invoice vendor",
                "po_document_id": str(po.id),
            }

        # Check PO total validity
        if po_total is None:
            return {
                "matched": False,
                "reason": "PO total is missing or invalid",
                "po_document_id": str(po.id),
            }

        # Check exact total match
        if invoice_total != po_total:
            return {
                "matched": False,
                "reason": "Invoice total does not match PO total",
                "po_document_id": str(po.id),
            }

        return {
            "matched": True,
            "reason": "Vendor, PO number, and total match",
            "po_document_id": str(po.id),
        }

    return {
        "matched": False,
        "reason": "Matching purchase order not found",
        "po_document_id": None,
    }


def match_invoice_three_way(db: Session, invoice: Invoice) -> dict:
    """
    Compare a persisted invoice with its current PO and posted receipts.
    Inputs: db (Session), invoice (Invoice).
    Output: dict with keys 'matched' (bool), 'requires_review' (bool), 'issues' (list[str]), 'po_id' (str | None), 'goods_receipt_ids' (list[str]).
    Effects: reads database rows from invoices, purchase_orders, po_lines, goods_receipts.
    Results: advisory 3-way check; returns findings for human review without modifying invoice state.
    """
    issues: list[str] = []
    # Step 1: Validate normalized invoice number and check duplicates
    invoice_no = getattr(invoice, "invoice_no_normalized", None) or (
        normalize_invoice_no(invoice.invoice_no)
    )
    if not invoice_no or not invoice.vendor_id:
        issues.append("Invoice number or resolved vendor is missing; review required")
    else:
        duplicate = (
            db.query(Invoice.id)
            .filter(
                Invoice.id != invoice.id,
                Invoice.is_current.is_(True),
                Invoice.vendor_id == invoice.vendor_id,
                Invoice.invoice_no_normalized == invoice_no,
            )
            .first()
        )
        if duplicate:
            issues.append("Duplicate invoice detected for this vendor")

    # Step 2: Validate PO linkage
    if not invoice.po_id:
        issues.append("Purchase order is missing or unresolved")
        return {
            "matched": False,
            "requires_review": True,
            "issues": issues,
            "po_id": None,
            "goods_receipt_ids": [],
        }

    po = db.get(PurchaseOrder, invoice.po_id)
    if po is None:
        issues.append("Referenced purchase order was not found")
        return {
            "matched": False,
            "requires_review": True,
            "issues": issues,
            "po_id": str(invoice.po_id),
            "goods_receipt_ids": [],
        }
    if (
        invoice.vendor_id is None
        or po.vendor_id is None
        or invoice.vendor_id != po.vendor_id
    ):
        issues.append("Invoice vendor does not match purchase order vendor")
    if invoice.po_number_raw and normalize_value(
        invoice.po_number_raw
    ) != normalize_value(po.po_number):
        issues.append("Invoice PO number does not match referenced purchase order")
    if (
        invoice.currency is None
        or po.currency is None
        or invoice.currency.strip().upper() != po.currency.strip().upper()
    ):
        issues.append("Invoice or PO currency is missing or does not match")
    if invoice.total_amount is None or po.total_amount is None:
        issues.append("Invoice or PO total is missing; review required")
    elif invoice.total_amount > po.total_amount:
        issues.append("Invoice total exceeds PO total")

    # Step 3: Query Goods Receipts linked to this PO
    receipts = (
        db.query(GoodsReceipt)
        .options(selectinload(GoodsReceipt.lines))
        .filter(GoodsReceipt.po_id == po.id)
        .all()
    )
    if not receipts:
        issues.append("No goods receipt is recorded for this purchase order")
        return {
            "matched": False,
            "requires_review": True,
            "issues": issues,
            "po_id": str(po.id),
            "goods_receipt_ids": [],
        }
    if any(r.vendor_id != po.vendor_id for r in receipts):
        issues.append("Goods receipt vendor does not match purchase order vendor")

    # Step 4: Compare PO lines and invoice lines against received quantities
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
    )
    invoice_lines = (
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    )
    if not po_lines or not invoice_lines:
        issues.append("PO or invoice line quantities are missing; review required")
    else:
        po_by_id = {line.id: line for line in po_lines}
        accepted: dict[object, Decimal] = {}
        received: dict[object, Decimal] = {}
        for receipt in receipts:
            for line in receipt.lines:
                accepted[line.po_line_id] = (
                    accepted.get(line.po_line_id, Decimal("0")) + line.accepted_qty
                )
                received[line.po_line_id] = (
                    received.get(line.po_line_id, Decimal("0")) + line.received_qty
                )
        for po_line in po_lines:
            po_qty = Decimal(str(po_line.qty or 0))
            if received.get(po_line.id, Decimal("0")) > po_qty:
                issues.append(
                    f"Over-receipt for PO line {po_line.item_code or po_line.id}"
                )
            if accepted.get(po_line.id, Decimal("0")) < po_qty:
                issues.append(
                    f"Partial receipt for PO line {po_line.item_code or po_line.id}"
                )
        for line in invoice_lines:
            line_po_id = line.po_line_id
            if line_po_id is None:
                candidates = [
                    p
                    for p in po_lines
                    if (
                        line.item_code
                        and normalize_value(p.item_code)
                        == normalize_value(line.item_code)
                    )
                    or (
                        not line.item_code
                        and line.description
                        and normalize_value(p.description)
                        == normalize_value(line.description)
                    )
                ]
                if len(candidates) != 1:
                    issues.append(
                        "Invoice line cannot be uniquely matched to a PO line; "
                        "review required"
                    )
                    continue
                line_po_id = candidates[0].id
            po_line = po_by_id.get(line_po_id)
            if po_line is None:
                issues.append(
                    "Invoice line references a PO line outside this purchase order"
                )
                continue
            qty = Decimal(str(line.qty or 0))
            if qty <= 0:
                issues.append(
                    "Invoice line quantity is missing or invalid; review required"
                )
            elif qty > accepted.get(line_po_id, Decimal("0")):
                issues.append(
                    "Invoice quantity exceeds accepted receipt quantity for PO "
                    f"line {po_line.item_code or po_line.id}"
                )
            if (
                qty > 0
                and line.line_amount is not None
                and po_line.unit_price is not None
            ):
                expected_amount = Decimal(str(po_line.unit_price)) * qty
                if abs(Decimal(str(line.line_amount)) - expected_amount) > Decimal(
                    "0.01"
                ):
                    issues.append(
                        "Invoice line amount does not match PO unit price for "
                        f"PO line {po_line.item_code or po_line.id}"
                    )
        if (
            all(
                accepted.get(p.id, Decimal("0")) >= Decimal(str(p.qty or 0))
                for p in po_lines
            )
            and invoice.total_amount is not None
            and po.total_amount is not None
            and abs(invoice.total_amount - po.total_amount) > Decimal("0.01")
        ):
            issues.append("Invoice total does not match fully received PO total")

    return {
        "matched": not issues,
        "requires_review": True,
        "issues": issues,
        "po_id": str(po.id),
        "goods_receipt_ids": [str(r.id) for r in receipts],
    }
