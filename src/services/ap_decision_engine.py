"""
Application module for deterministic AP matching decision rules.
Flow: evaluate 2-way and 3-way matching rules using strict Decimal math, detect discrepancies, and enforce tolerances.
Called by: invoice_matching_services, celery worker tasks.
Calls: invoice_confidence_services, invoice_number_services, po_matching_services, llm_matching_services.
Tables: invoices, invoice_lines, purchase_orders, po_lines, goods_receipts, gr_lines.
"""

import logging
import os
from decimal import Decimal

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.document_workflow_services import _owner_scope
from src.services.invoice_confidence_services import overall_confidence_percent
from src.services.invoice_number_services import find_duplicate_invoice
from src.services.po_matching_services import normalize_value

# Configurable matching rule parameters and decimal tolerances
RULE_SET_VERSION = os.getenv("AP_MATCH_RULE_SET_VERSION", "ap-match-v1")
PRICE_TOLERANCE = Decimal(os.getenv("AP_PRICE_TOLERANCE_PERCENT", "2")) / 100
TAX_TOLERANCE = Decimal(os.getenv("TAX_TOLERANCE", "0.01"))
MIN_MATERIAL_CONFIDENCE = Decimal(os.getenv("AP_MATCH_MIN_CONFIDENCE", "85"))
MATERIAL_FIELDS = (
    "vendor",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax",
    "total",
    "order_number",
    "gl_code",
)


def _d(value) -> Decimal | None:
    """Safely convert any numeric or string value to Decimal, returning None on error."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _score(raw) -> Decimal | None:
    """Extract and scale raw confidence values to 0-100 range."""
    value = raw.get("confidence") if isinstance(raw, dict) else raw
    try:
        score = Decimal(str(value))
        return score * 100 if Decimal(0) <= score <= Decimal(1) else score
    except Exception:
        return None


def _issue(
    code,
    message,
    actual=None,
    expected=None,
    difference=None,
    tolerance=None,
    line=None,
    action="Review the invoice and supporting records.",
) -> dict:
    """Format a standardized discrepancy dictionary for persistence and frontend UI rendering."""
    return {
        "code": code,
        "message": message,
        "invoice_value": actual,
        "expected_value": expected,
        "difference": difference,
        "tolerance": tolerance,
        "line_reference": line,
        "next_action": action,
    }


def _json_safe(value):
    """Recursively convert Decimal objects to strings for JSON serialization."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def evaluate_invoice(db: Session, invoice: Invoice, owner_id=None) -> dict:
    """
    Evaluate an invoice against procurement records using deterministic decimal matching rules.
    Inputs: db (Session), invoice (Invoice), owner_id (UUID | None).
    Output: dict - outcome ('ready_to_pay' or 'needs_review'), confidence metrics, discrepancy list, inputs summary.
    Effects: reads purchase_orders, po_lines, goods_receipts, gr_lines, invoice_lines.
    Results: powers automated matching decisions and human review flags.
    """
    # Step 1: Delegate to LLM matching engine if enabled, falling back to deterministic rule engine
    try:
        from src.services.llm_matching_services import llm_evaluate_invoice_match
        llm_decision = llm_evaluate_invoice_match(db, invoice, owner_id=owner_id)
        if llm_decision is not None:
            return llm_decision
    except Exception as exc:
        logger.warning("LLM matching evaluation failed, falling back to rule engine: %s", exc)

    issues = []
    po_rows = []
    po_number_raw = getattr(invoice, "po_number_raw", None)
    po_ref = normalize_value(po_number_raw)

    # Step 2: Validate PO existence and check for ambiguous duplicates
    if po_ref:
        po_rows = [
            po
            for po in db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.is_current.is_(True),
                *_owner_scope(PurchaseOrder, owner_id),
            )
            .all()
            if normalize_value(po.po_number) == po_ref
        ]
    po = po_rows[0] if len(po_rows) == 1 else None
    if not po_rows:
        issues.append(
            _issue(
                "MISSING_PO",
                "No purchase order matches this invoice.",
                po_number_raw,
                None,
                action="Add or correct the PO number.",
            )
        )
    elif len(po_rows) != 1:
        issues.append(
            _issue(
                "AMBIGUOUS_PO",
                "More than one purchase order matches this number.",
                po_number_raw,
                len(po_rows),
                action="Resolve the duplicate PO records.",
            )
        )

    # Step 3: Check invoice line item presence
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    if not lines:
        issues.append(
            _issue(
                "LINE_NOT_ON_PO",
                "No invoice lines are available to match against the PO.",
                None,
                "At least one matched PO line",
                action="Verify the extracted invoice line items.",
            )
        )
    po_lines = (
        po.lines if (po and getattr(po, "lines", None) is not None)
        else (db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all() if po else [])
    )

    # Step 4: Check Goods Receipt existence and calculate cumulative accepted quantities
    receipts = (
        db.query(GoodsReceipt)
        .filter(GoodsReceipt.po_id == po.id, *_owner_scope(GoodsReceipt, owner_id))
        .all()
        if po
        else []
    )
    if not receipts:
        issues.append(
            _issue(
                "MISSING_GR",
                "No goods receipt is recorded for this PO.",
                None,
                "Goods receipt",
                action="Record or link a goods receipt.",
            )
        )
    received = {}
    if receipts:
        for grline in (
            db.query(GoodsReceiptLine)
            .filter(GoodsReceiptLine.goods_receipt_id.in_([g.id for g in receipts]))
            .all()
        ):
            received[grline.po_line_id] = received.get(
                grline.po_line_id, Decimal(0)
            ) + _d(grline.accepted_qty)

    # Step 5: Check vendor alignment
    invoice_vendor_id = getattr(invoice, "vendor_id", None)
    if po and (not invoice_vendor_id or invoice_vendor_id != po.vendor_id):
        issues.append(
            _issue(
                "VENDOR_MISMATCH",
                "Invoice vendor does not match the PO vendor.",
                str(invoice_vendor_id),
                str(po.vendor_id),
                action="Correct the vendor or PO assignment.",
            )
        )

    # Step 6: Check strict currency matching (no auto-conversion)
    currency = getattr(invoice, "currency", None)
    if po and (
        not currency or not po.currency or currency.upper() != po.currency.upper()
    ):
        issues.append(
            _issue(
                "CURRENCY_MISMATCH",
                "Invoice and PO currencies must match.",
                currency,
                po.currency,
                action="Correct the currency; amounts are never converted.",
            )
        )

    # Step 7: Check line items (arithmetic, SKU mapping, quantity vs accepted, price vs tolerance)
    for index, line in enumerate(lines, 1):
        reference = line.item_code or line.description or str(index)
        qty, price = _d(line.qty), _d(line.unit_price)
        computed = qty * price if qty is not None and price is not None else None
        amount = _d(line.line_amount)
        # Check qty * unit_price == line_amount
        if computed is not None and (amount is None or computed != amount):
            issues.append(
                _issue(
                    "LINE_TOTAL_VARIANCE",
                    "Line total does not equal quantity times unit price.",
                    amount,
                    computed,
                    amount - computed if amount is not None else None,
                    Decimal("0"),
                    reference,
                    "Correct the quantity, price, or line total.",
                )
            )
        candidates = [
            pl
            for pl in po_lines
            if normalize_value(line.item_code)
            and normalize_value(pl.item_code) == normalize_value(line.item_code)
        ]
        # Check if line SKU exists on the PO
        if not candidates:
            issues.append(
                _issue(
                    "LINE_NOT_ON_PO",
                    "Invoice line has no matching PO SKU.",
                    reference,
                    [pl.item_code for pl in po_lines],
                    line=reference,
                    action=(
                        "Review the SKU; description similarity is only a suggestion."
                    ),
                )
            )
            continue
        pl = candidates[0]
        po_price = _d(pl.unit_price)
        # Check billed qty <= received accepted qty on GRN
        if qty is not None and received.get(pl.id, Decimal(0)) < qty:
            issues.append(
                _issue(
                    "QUANTITY_VARIANCE",
                    "Invoice quantity exceeds accepted quantity received.",
                    qty,
                    received.get(pl.id, Decimal(0)),
                    qty - received.get(pl.id, Decimal(0)),
                    line=reference,
                    action="Verify the invoice quantity or receipt.",
                )
            )
        # Check unit price against allowed percentage tolerance (default 2%)
        if price is None or po_price is None:
            issues.append(
                _issue(
                    "PRICE_VARIANCE",
                    "Invoice or PO unit price is missing.",
                    price,
                    po_price,
                    line=reference,
                    action="Verify the invoice and PO unit prices.",
                )
            )
        else:
            delta = abs(price - po_price)
            allowed = abs(po_price) * PRICE_TOLERANCE
            if delta > allowed:
                issues.append(
                    _issue(
                        "PRICE_VARIANCE",
                        "Invoice unit price is outside the allowed PO tolerance.",
                        price,
                        po_price,
                        price - po_price,
                        allowed,
                        reference,
                        "Review the invoice price or obtain PO approval.",
                    )
                )

    # Step 8: Check tax amount within tolerance
    expected_tax = _d(po.tax_amount) if po else None
    actual_tax = _d(getattr(invoice, "tax_amount", None))
    if po and (
        expected_tax is None
        or actual_tax is None
        or abs(actual_tax - expected_tax) > TAX_TOLERANCE
    ):
        issues.append(
            _issue(
                "TAX_VARIANCE",
                "Invoice tax differs from the PO tax beyond tolerance.",
                actual_tax,
                expected_tax,
                actual_tax - expected_tax if actual_tax is not None else None,
                TAX_TOLERANCE,
                action="Review tax amounts and tax treatment.",
            )
        )

    # Step 9: Reconcile subtotal, tax, and header totals
    subtotal, total, tax = (
        _d(getattr(invoice, "subtotal", None)),
        _d(getattr(invoice, "total_amount", None)),
        _d(getattr(invoice, "tax_amount", None)),
    )
    expected_total = (
        subtotal + tax if subtotal is not None and tax is not None else None
    )
    line_amounts = [_d(line.line_amount) for line in lines]
    computed_subtotal = (
        sum(line_amounts, Decimal(0))
        if line_amounts and all(value is not None for value in line_amounts)
        else None
    )
    # Check lines sum == subtotal
    if (
        subtotal is not None
        and computed_subtotal is not None
        and abs(subtotal - computed_subtotal) > TAX_TOLERANCE
    ):
        issues.append(
            _issue(
                "HEADER_TOTAL_VARIANCE",
                "Invoice subtotal does not reconcile to its line totals.",
                subtotal,
                computed_subtotal,
                subtotal - computed_subtotal,
                TAX_TOLERANCE,
                action="Correct the invoice subtotal or line totals.",
            )
        )
    # Check subtotal + tax == total
    if (
        subtotal is None
        or tax is None
        or total is None
        or abs(total - (subtotal + tax)) > TAX_TOLERANCE
    ):
        issues.append(
            _issue(
                "HEADER_TOTAL_VARIANCE",
                "Invoice total does not reconcile to subtotal plus tax.",
                total,
                expected_total,
                total - expected_total
                if total is not None and expected_total is not None
                else None,
                TAX_TOLERANCE,
                action="Correct the invoice header totals.",
            )
        )

    # Step 10: Check duplicate invoice number for this vendor
    if (
        invoice_vendor_id
        and getattr(invoice, "invoice_no", None)
        and find_duplicate_invoice(
            db,
            invoice_vendor_id,
            getattr(invoice, "invoice_no_normalized", None),
            exclude_invoice_id=invoice.id,
        )
    ):
        issues.append(
            _issue(
                "POSSIBLE_DUPLICATE",
                "Another invoice has this vendor and invoice number.",
                getattr(invoice, "invoice_no", None),
                "Unique vendor and invoice number",
                action="Check the existing invoice before proceeding.",
            )
        )

    # Step 11: Evaluate field confidence thresholds
    confidence = getattr(invoice, "field_confidence", None) or {}
    overall_percent = overall_confidence_percent(confidence)
    low_fields = []
    for field in MATERIAL_FIELDS:
        score = _score(confidence.get(field))
        if score is None or score < MIN_MATERIAL_CONFIDENCE:
            low_fields.append(field)
    for index, line in enumerate(lines, 1):
        fields = getattr(line, "field_confidence", None) or {}
        for field in ("qty", "unit_price", "line_amount"):
            score = _score(fields.get(field))
            if score is None or score < MIN_MATERIAL_CONFIDENCE:
                low_fields.append(f"line_{index}.{field}")
    if low_fields or Decimal(overall_percent) < MIN_MATERIAL_CONFIDENCE:
        issues.append(
            _issue(
                "LOW_CONFIDENCE",
                "One or more material fields are unverified or below the "
                "confidence threshold.",
                low_fields,
                f"At least {MIN_MATERIAL_CONFIDENCE}% confidence",
                action="Verify the listed fields.",
            )
        )
    confidence_levels = {}
    for field in MATERIAL_FIELDS:
        score = _score(confidence.get(field))
        confidence_levels[field] = (
            "low"
            if score is None
            else "high"
            if score >= 90
            else "medium"
            if score >= 75
            else "low"
        )
    for index, line in enumerate(lines, 1):
        fields = getattr(line, "field_confidence", None) or {}
        for field in ("qty", "unit_price", "line_amount"):
            score = _score(fields.get(field))
            confidence_levels[f"line_{index}.{field}"] = (
                "low"
                if score is None
                else "high"
                if score >= 90
                else "medium"
                if score >= 75
                else "low"
            )
    confidence_level = (
        "low"
        if any(value == "low" for value in confidence_levels.values())
        else "medium"
        if any(value == "medium" for value in confidence_levels.values())
        else "high"
    )

    # Step 12: Assemble final outcome payload
    return {
        "rule_set_version": RULE_SET_VERSION,
        "outcome": "ready_to_pay" if not issues else "needs_review",
        "confidence_level": confidence_level,
        "overall_confidence_percent": overall_percent,
        "field_confidence": confidence,
        "confidence_levels": confidence_levels,
        "low_confidence_fields": low_fields,
        "discrepancies": _json_safe(issues),
        "inputs_summary": {
            "invoice_id": str(invoice.id),
            "po_number": po_number_raw,
            "vendor_id": str(invoice_vendor_id) if invoice_vendor_id else None,
            "invoice_number": getattr(invoice, "invoice_no", None),
            "currency": currency,
            "invoice_total": str(getattr(invoice, "total_amount", None)),
            "invoice_subtotal": str(getattr(invoice, "subtotal", None)),
            "invoice_tax": str(getattr(invoice, "tax_amount", None)),
            "field_confidence": confidence,
            "po_ids": [str(row.id) for row in po_rows],
            "receipt_ids": [str(row.id) for row in receipts],
            "po_currency": po.currency if po else None,
            "po_tax": str(po.tax_amount) if po else None,
            "po_total": str(po.total_amount) if po else None,
            "invoice_lines": [
                {
                    "sku": line.item_code,
                    "quantity": str(line.qty),
                    "unit_price": str(line.unit_price),
                    "line_total": str(line.line_amount),
                    "field_confidence": getattr(line, "field_confidence", None) or {},
                }
                for line in lines
            ],
            "po_lines": [
                {
                    "sku": line.item_code,
                    "quantity": str(line.qty),
                    "unit_price": str(line.unit_price),
                }
                for line in po_lines
            ],
            "accepted_quantity_by_po_line": {
                str(line_id): str(quantity) for line_id, quantity in received.items()
            },
        },
    }
