"""
Application module for deterministic AP matching decision rules.
Flow: evaluate 2-way and 3-way matching rules using strict Decimal math, detect discrepancies, and enforce tolerances.
Called by: invoice_matching_services, celery worker tasks.
Calls: invoice_confidence_services, invoice_number_services, po_matching_services, llm_matching_services.
Tables: invoices, invoice_lines, purchase_orders, po_lines, goods_receipts, gr_lines.
"""

import logging
import os
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor
from src.services.document_workflow_services import _owner_scope
from src.services.invoice_confidence_services import overall_confidence_percent
from src.services.invoice_number_services import find_duplicate_invoice
from src.services.po_matching_services import normalize_value
from src.services.tolerance_config_services import (
    read_nonnegative_decimal,
    tolerance_fraction,
)

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


def _d(value: Any) -> Decimal | None:
    """Safely convert any numeric or string value to Decimal, returning None on error."""
    if value is None or isinstance(value, bool):
        return None
    try:
        res = Decimal(str(value))
        return res if res.is_finite() else None
    except Exception:
        return None


def _score(raw: Any) -> Decimal | None:
    """Extract and scale raw confidence values to 0-100 range."""
    value = raw.get("confidence") if isinstance(raw, dict) else raw
    try:
        score = Decimal(str(value))
        return score * 100 if Decimal(0) <= score <= Decimal(1) else score
    except Exception:
        return None


def _issue(
    code: str,
    message: str,
    actual: Any = None,
    expected: Any = None,
    difference: Any = None,
    tolerance: Any = None,
    line: Any = None,
    action: str = "Review the invoice and supporting records.",
) -> dict[str, Any]:
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


def _json_safe(value: Any) -> Any:
    """Recursively convert Decimal objects to strings for JSON serialization."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def get_configured_price_tolerance() -> Decimal:
    """Read configured price tolerance percentage fraction (default 2%)."""
    raw = os.getenv("INVOICE_UNIT_PRICE_TOLERANCE_PCT") or os.getenv("AP_PRICE_TOLERANCE_PERCENT", "2")
    try:
        val = Decimal(raw)
        return val / Decimal(100) if val > Decimal(0.1) else val
    except Exception:
        return Decimal("0.02")


def get_configured_tax_tolerance() -> Decimal:
    """Read configured absolute tax tolerance (default 0.01)."""
    raw = os.getenv("TAX_TOLERANCE_ABS") or os.getenv("TAX_TOLERANCE", "0.01")
    try:
        return Decimal(raw)
    except Exception:
        return Decimal("0.01")


# ---------------------------------------------------------------------------
# Pure Deterministic Calculation Functions (Decimal Arithmetic Only)
# ---------------------------------------------------------------------------


def validate_line_arithmetic(
    qty: Decimal | None,
    unit_price: Decimal | None,
    line_amount: Decimal | None,
    reference: str,
) -> dict[str, Any] | None:
    """
    Deterministically validate: qty * unit_price == line_amount.
    No LLM involvement.
    """
    if qty is not None and unit_price is not None:
        expected_amount = (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if line_amount is not None and abs(line_amount - expected_amount) > Decimal("0.01"):
            diff = (line_amount - expected_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return _issue(
                "LINE_TOTAL_VARIANCE",
                "Line total does not equal quantity times unit price.",
                actual=line_amount,
                expected=expected_amount,
                difference=diff,
                tolerance=Decimal("0.00"),
                line=reference,
                action="Correct the quantity, price, or line total.",
            )
    return None


def validate_price_variance(
    invoice_price: Decimal | None,
    po_price: Decimal | None,
    tolerance_pct: Decimal,
    reference: str,
) -> dict[str, Any] | None:
    """
    Deterministically validate unit price within allowed tolerance of PO unit price.
    No LLM involvement.
    """
    if invoice_price is None or po_price is None:
        return _issue(
            "PRICE_VARIANCE",
            "Invoice or PO unit price is missing.",
            actual=invoice_price,
            expected=po_price,
            line=reference,
            action="Verify the invoice and PO unit prices.",
        )
    delta = abs(invoice_price - po_price)
    allowed = (abs(po_price) * tolerance_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if delta > allowed:
        diff = (invoice_price - po_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return _issue(
            "PRICE_VARIANCE",
            "Invoice unit price is outside the allowed PO tolerance.",
            actual=invoice_price,
            expected=po_price,
            difference=diff,
            tolerance=allowed,
            line=reference,
            action="Review the invoice price or obtain PO approval.",
        )
    return None


def validate_quantity_variance(
    invoice_qty: Decimal | None,
    expected_qty: Decimal,
    reference: str,
    context: str = "receipt",
) -> dict[str, Any] | None:
    """
    Deterministically validate: billed quantity <= expected quantity (receipt or PO).
    Returns discrepancy ONLY when invoice quantity exceeds expected quantity.
    No LLM involvement.
    """
    if invoice_qty is not None and invoice_qty > expected_qty:
        diff = (invoice_qty - expected_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        msg = (
            "Invoice quantity exceeds accepted quantity received."
            if context == "receipt"
            else "Invoice quantity exceeds purchase order quantity."
        )
        action = (
            "Verify the invoice quantity or receipt."
            if context == "receipt"
            else "Verify the invoice quantity against the purchase order."
        )
        return _issue(
            "QUANTITY_VARIANCE",
            msg,
            actual=invoice_qty,
            expected=expected_qty,
            difference=diff,
            line=reference,
            action=action,
        )
    return None


def validate_tax_variance(
    actual_tax: Decimal | None,
    expected_tax: Decimal | None,
    tolerance_abs: Decimal,
) -> dict[str, Any] | None:
    """
    Deterministically validate: abs(actual_tax - expected_tax) <= tolerance_abs.
    Returns discrepancy ONLY when an actual tax variance exceeds tolerance.
    Does not flag variance when neither PO nor invoice specifies tax or both are 0.
    No LLM involvement.
    """
    # If neither document specifies tax, or both are zero, there is no variance
    if (expected_tax is None or expected_tax == Decimal("0")) and (actual_tax is None or actual_tax == Decimal("0")):
        return None

    if expected_tax is None or actual_tax is None or abs(actual_tax - expected_tax) > tolerance_abs:
        diff = (
            (actual_tax - expected_tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if actual_tax is not None and expected_tax is not None
            else None
        )
        return _issue(
            "TAX_VARIANCE",
            "Invoice tax differs from the PO tax beyond tolerance.",
            actual=actual_tax,
            expected=expected_tax,
            difference=diff,
            tolerance=tolerance_abs,
            action="Review tax amounts and tax treatment.",
        )
    return None


def validate_subtotal_reconciliation(
    subtotal: Decimal | None,
    line_amounts: list[Decimal | None],
    tolerance_abs: Decimal,
) -> dict[str, Any] | None:
    """
    Deterministically validate: sum of line totals == subtotal.
    Returns discrepancy ONLY when subtotal does not match line amounts.
    No LLM involvement.
    """
    computed_subtotal = (
        sum(line_amounts, Decimal("0"))
        if line_amounts and all(v is not None for v in line_amounts)
        else None
    )
    if subtotal is not None and computed_subtotal is not None and abs(subtotal - computed_subtotal) > tolerance_abs:
        diff = (subtotal - computed_subtotal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return _issue(
            "HEADER_TOTAL_VARIANCE",
            "Invoice subtotal does not reconcile to its line totals.",
            actual=subtotal,
            expected=computed_subtotal,
            difference=diff,
            tolerance=tolerance_abs,
            action="Correct the invoice subtotal or line totals.",
        )
    return None


def validate_header_totals(
    total: Decimal | None,
    subtotal: Decimal | None,
    tax: Decimal | None,
    tolerance_abs: Decimal,
) -> dict[str, Any] | None:
    """
    Deterministically validate: subtotal + tax == total.
    Returns discrepancy ONLY when total fails to reconcile.
    No LLM involvement.
    """
    # If tax is None but subtotal and total are present and reconcile within tolerance, tax is zero
    effective_tax = tax if tax is not None else Decimal("0")
    if subtotal is None or total is None:
        return _issue(
            "HEADER_TOTAL_VARIANCE",
            "Invoice total does not reconcile to subtotal plus tax.",
            actual=total,
            expected=None,
            tolerance=tolerance_abs,
            action="Correct the invoice header totals.",
        )

    expected_total = subtotal + effective_tax
    if abs(total - expected_total) > tolerance_abs:
        diff = (total - expected_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return _issue(
            "HEADER_TOTAL_VARIANCE",
            "Invoice total does not reconcile to subtotal plus tax.",
            actual=total,
            expected=expected_total,
            difference=diff,
            tolerance=tolerance_abs,
            action="Correct the invoice header totals.",
        )
    return None


def aggregate_accepted_quantities(
    receipt_lines: list[GoodsReceiptLine],
    po_lines: list[PurchaseOrderLine] | None = None,
) -> dict[Any, Decimal]:
    """
    Deterministically calculate cumulative accepted quantity per PO line from goods receipts.
    Resolves po_line_id by SKU if unlinked on the receipt line.
    No LLM involvement.
    """
    accepted: dict[Any, Decimal] = {}
    po_line_by_code: dict[str, Any] = {}
    if po_lines:
        for pl in po_lines:
            code = normalize_value(getattr(pl, "item_code", None))
            if code:
                po_line_by_code[code] = pl.id

    for grl in receipt_lines:
        acc_val = _d(getattr(grl, "accepted_qty", None))
        if acc_val is None:
            rec_val = _d(getattr(grl, "received_qty", None)) or Decimal("0")
            rej_val = _d(getattr(grl, "rejected_qty", None)) or Decimal("0")
            acc_val = max(Decimal("0"), rec_val - rej_val)
        po_line_id = getattr(grl, "po_line_id", None)
        if po_line_id is None and po_line_by_code:
            gr_code = normalize_value(getattr(grl, "item_code", None))
            if gr_code in po_line_by_code:
                po_line_id = po_line_by_code[gr_code]
        if po_line_id is not None:
            accepted[po_line_id] = accepted.get(po_line_id, Decimal("0")) + acc_val
    return accepted


# ---------------------------------------------------------------------------
# Core Deterministic Matching & Business Rules Evaluator
# ---------------------------------------------------------------------------


def execute_deterministic_matching(
    db: Session | None,
    invoice: Invoice,
    lines: list[InvoiceLine],
    po: PurchaseOrder | None,
    po_lines: list[PurchaseOrderLine],
    receipts: list[GoodsReceipt],
    receipt_lines: list[GoodsReceiptLine],
    line_mappings: list[dict[str, Any]] | None = None,
    owner_id: Any = None,
    llm_discrepancies: list[dict[str, Any]] | None = None,
    llm_summary: str = "",
    rule_set_version: str | None = None,
    price_tolerance_pct: Decimal | None = None,
    tax_tolerance_abs: Decimal | None = None,
) -> dict[str, Any]:
    """
    Execute all deterministic financial calculations and business rule enforcement.
    The LLM may have proposed candidate relationships or line pairings, but Python
    strictly validates all IDs, executes all arithmetic, and determines final status.
    """
    price_tol = price_tolerance_pct if price_tolerance_pct is not None else get_configured_price_tolerance()
    tax_tol = tax_tolerance_abs if tax_tolerance_abs is not None else get_configured_tax_tolerance()
    ver = rule_set_version or RULE_SET_VERSION

    issues: list[dict[str, Any]] = []
    checks_performed = 0

    # 1. Validate PO Existence
    checks_performed += 1
    if po is None:
        issues.append(
            _issue(
                "MISSING_PO",
                "No purchase order matches this invoice.",
                actual=getattr(invoice, "po_number_raw", None),
                expected=None,
                action="Add or correct the PO number.",
            )
        )

    # 2. Check Line Presence
    checks_performed += 1
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

    # 3. Check Goods Receipt existence and aggregate accepted quantities
    checks_performed += 1
    accepted_by_po_line = aggregate_accepted_quantities(receipt_lines, po_lines=po_lines)
    if po is not None and not receipts:
        issues.append(
            _issue(
                "MISSING_GR",
                "No goods receipt is recorded for this PO.",
                None,
                "Goods receipt",
                action="Record or link a goods receipt.",
            )
        )

    # 4. Check Vendor Alignment
    checks_performed += 1
    invoice_vendor_id = getattr(invoice, "vendor_id", None)
    if po is not None and getattr(po, "vendor_id", None):
        if not invoice_vendor_id or invoice_vendor_id != po.vendor_id:
            issues.append(
                _issue(
                    "VENDOR_MISMATCH",
                    "Invoice vendor does not match the PO vendor.",
                    actual=str(invoice_vendor_id) if invoice_vendor_id else None,
                    expected=str(po.vendor_id),
                    action="Correct the vendor or PO assignment.",
                )
            )

    # 5. Check Currency Equality (No auto-conversion)
    checks_performed += 1
    currency = getattr(invoice, "currency", None)
    po_curr = getattr(po, "currency", None) if po else None
    if po is not None and (not currency or not po_curr or currency.strip().upper() != po_curr.strip().upper()):
        issues.append(
            _issue(
                "CURRENCY_MISMATCH",
                "Invoice and PO currencies must match.",
                actual=currency,
                expected=po_curr,
                action="Correct the currency; amounts are never converted.",
            )
        )

    # 6. Validate Invoice Lines (Arithmetic, Semantic Line Mapping Validation, Price & Quantity Variances)
    po_line_by_id = {pl.id: pl for pl in po_lines}
    po_line_by_code = {
        normalize_value(getattr(pl, "item_code", None)): pl
        for pl in po_lines
        if getattr(pl, "item_code", None) and normalize_value(getattr(pl, "item_code", None))
    }

    for index, line in enumerate(lines, 1):
        checks_performed += 1
        reference = getattr(line, "item_code", None) or getattr(line, "description", None) or str(index)
        qty, price = _d(getattr(line, "qty", None)), _d(getattr(line, "unit_price", None))
        amount = _d(getattr(line, "line_amount", None))

        # 6a. Arithmetic check: qty * price == amount
        arith_issue = validate_line_arithmetic(qty, price, amount, reference)
        if arith_issue:
            issues.append(arith_issue)

        if po is None:
            continue

        # 6b. Validate LLM proposed line mapping or resolve fallback
        matched_pl: PurchaseOrderLine | None = None
        # Try LLM proposed mappings first, verifying they belong to this PO and do not contradict SKUs
        for m in line_mappings or []:
            if isinstance(m, dict):
                m_inv = str(m.get("invoice_line_id", "")).strip()
                m_po = m.get("matched_po_line_id")
                line_id_str = str(getattr(line, "id", ""))
                if m_inv in (line_id_str, str(index)) and m_po:
                    try:
                        p_uuid = uuid.UUID(str(m_po))
                        if p_uuid in po_line_by_id:
                            cand = po_line_by_id[p_uuid]
                            # Anti-hallucination check: if both have explicit SKUs, reject conflicting mappings
                            inv_sku = normalize_value(getattr(line, "item_code", None))
                            po_sku = normalize_value(getattr(cand, "item_code", None))
                            if inv_sku and po_sku and inv_sku != po_sku:
                                logger.warning(
                                    "Rejecting LLM line mapping: invoice SKU '%s' contradicts PO SKU '%s'",
                                    inv_sku, po_sku
                                )
                            else:
                                matched_pl = cand
                                break
                    except Exception:
                        pass

        # Fallback 1: Exact SKU match
        item_code = getattr(line, "item_code", None)
        if matched_pl is None and item_code:
            norm_code = normalize_value(item_code)
            if norm_code in po_line_by_code:
                matched_pl = po_line_by_code[norm_code]

        # Fallback 2: Description match (when item_code is absent or unindexed)
        line_desc = getattr(line, "description", None)
        if matched_pl is None and not item_code and line_desc:
            line_desc_norm = normalize_value(line_desc)
            desc_candidates = [
                pl for pl in po_lines
                if getattr(pl, "description", None) and (
                    normalize_value(pl.description) == line_desc_norm
                    or line_desc_norm in normalize_value(pl.description)
                    or normalize_value(pl.description) in line_desc_norm
                )
            ]
            if len(desc_candidates) == 1:
                matched_pl = desc_candidates[0]

        # Fallback 3: 1-to-1 match (only when item_code is absent)
        if matched_pl is None and not item_code and len(po_lines) == 1 and len(lines) == 1:
            matched_pl = po_lines[0]

        # Line not on PO check
        if matched_pl is None:
            issues.append(
                _issue(
                    "LINE_NOT_ON_PO",
                    "Invoice line has no matching PO SKU.",
                    actual=reference,
                    expected=[getattr(pl, "item_code", None) for pl in po_lines if getattr(pl, "item_code", None)],
                    line=reference,
                    action="Review the SKU; description similarity is only a suggestion.",
                )
            )
            continue

        # 6c. Unit Price Variance check
        po_price = _d(getattr(matched_pl, "unit_price", None))
        price_issue = validate_price_variance(price, po_price, price_tol, reference)
        if price_issue:
            issues.append(price_issue)

        # 6d. Quantity Variance check:
        # In 3-way matching (receipts recorded), validate against accepted receipt quantities
        if receipts and qty is not None:
            accepted_qty = accepted_by_po_line.get(matched_pl.id, Decimal("0"))
            qty_issue = validate_quantity_variance(qty, accepted_qty, reference, context="receipt")
            if qty_issue:
                issues.append(qty_issue)
        elif not receipts and qty is not None and matched_pl is not None:
            # In 2-way matching (no receipts), validate against PO line quantity
            po_qty = _d(getattr(matched_pl, "qty", None))
            if po_qty is not None:
                qty_issue = validate_quantity_variance(qty, po_qty, reference, context="po")
                if qty_issue:
                    issues.append(qty_issue)

    # 7. Header Calculations (Tax Variance, Subtotal Reconciliation, Total Reconciliation)
    expected_tax = _d(getattr(po, "tax_amount", None)) if po else None
    actual_tax = _d(getattr(invoice, "tax_amount", None))
    if po is not None:
        checks_performed += 1
        tax_issue = validate_tax_variance(actual_tax, expected_tax, tax_tol)
        if tax_issue:
            issues.append(tax_issue)

    checks_performed += 1
    subtotal = _d(getattr(invoice, "subtotal", None))
    total = _d(getattr(invoice, "total_amount", None))
    line_amounts = [_d(getattr(l, "line_amount", None)) for l in lines]
    subtotal_issue = validate_subtotal_reconciliation(subtotal, line_amounts, tax_tol)
    if subtotal_issue:
        issues.append(subtotal_issue)

    checks_performed += 1
    header_issue = validate_header_totals(total, subtotal, actual_tax, tax_tol)
    if header_issue:
        issues.append(header_issue)

    # 8. Check Duplicate Invoice
    checks_performed += 1
    if (
        db is not None
        and invoice_vendor_id
        and getattr(invoice, "invoice_no", None)
        and find_duplicate_invoice(
            db,
            invoice_vendor_id,
            getattr(invoice, "invoice_no_normalized", None),
            exclude_invoice_id=getattr(invoice, "id", None),
        )
    ):
        issues.append(
            _issue(
                "POSSIBLE_DUPLICATE",
                "Another invoice has this vendor and invoice number.",
                actual=getattr(invoice, "invoice_no", None),
                expected="Unique vendor and invoice number",
                action="Check the existing invoice before proceeding.",
            )
        )

    # 9. Evaluate Field Confidence Thresholds
    checks_performed += 1
    confidence = getattr(invoice, "field_confidence", None) or {}
    overall_percent = overall_confidence_percent(confidence)
    low_fields = []
    for field in MATERIAL_FIELDS:
        if field == "gl_code" and not getattr(invoice, "gl_code", None) and field not in confidence:
            continue
        score_val = _score(confidence.get(field))
        if score_val is None or score_val < MIN_MATERIAL_CONFIDENCE:
            low_fields.append(field)

    for index, line in enumerate(lines, 1):
        fields = getattr(line, "field_confidence", None) or {}
        for field in ("qty", "unit_price", "line_amount"):
            score_val = _score(fields.get(field))
            if score_val is None or score_val < MIN_MATERIAL_CONFIDENCE:
                low_fields.append(f"line_{index}.{field}")

    if low_fields or Decimal(overall_percent) < MIN_MATERIAL_CONFIDENCE:
        issues.append(
            _issue(
                "LOW_CONFIDENCE",
                "One or more material fields are unverified or below the confidence threshold.",
                actual=low_fields,
                expected=f"At least {MIN_MATERIAL_CONFIDENCE}% confidence",
                action="Verify the listed fields.",
            )
        )

    confidence_levels = {}
    for field in MATERIAL_FIELDS:
        score_val = _score(confidence.get(field))
        confidence_levels[field] = (
            "low"
            if score_val is None
            else "high"
            if score_val >= 90
            else "medium"
            if score_val >= 75
            else "low"
        )
    for index, line in enumerate(lines, 1):
        fields = getattr(line, "field_confidence", None) or {}
        for field in ("qty", "unit_price", "line_amount"):
            score_val = _score(fields.get(field))
            confidence_levels[f"line_{index}.{field}"] = (
                "low"
                if score_val is None
                else "high"
                if score_val >= 90
                else "medium"
                if score_val >= 75
                else "low"
            )
    confidence_tier = (
        "low"
        if any(v == "low" for v in confidence_levels.values())
        else "medium"
        if any(v == "medium" for v in confidence_levels.values())
        else "high"
    )

    # 10. Enrich Discrepancies with LLM Contextual Explanations (without modifying calculations)
    if llm_discrepancies:
        llm_action_map = {
            d.get("code"): d.get("next_action")
            for d in llm_discrepancies
            if isinstance(d, dict) and d.get("code") and d.get("next_action")
        }
        for iss in issues:
            c = iss.get("code")
            if c in llm_action_map and llm_action_map[c]:
                iss["next_action"] = llm_action_map[c]

    # 11. Final Status Enforcement (Python Strict Rules)
    # The LLM CAN NEVER independently approve an invoice. Python enforces all requirements.
    procurement_issues = [
        item for item in issues
        if item.get("code") in {
            "MISSING_PO",
            "AMBIGUOUS_PO",
            "MISSING_GR",
            "VENDOR_MISMATCH",
            "CURRENCY_MISMATCH",
            "LINE_NOT_ON_PO",
            "QUANTITY_VARIANCE",
            "PRICE_VARIANCE",
            "LINE_TOTAL_VARIANCE",
            "TAX_VARIANCE",
            "HEADER_TOTAL_VARIANCE",
            "POSSIBLE_DUPLICATE",
        }
    ]
    is_clean_match = (
        not procurement_issues
        and not low_fields
        and bool(po)
        and bool(receipts)
        and len(lines) > 0
    )

    if is_clean_match:
        outcome = "ready_to_pay"
        score = 100
        confidence_level = "high"
    else:
        if any(item.get("code") == "MISSING_PO" for item in issues):
            outcome = "no_match"
            score = 0
            confidence_level = "low"
        else:
            outcome = "needs_review"
            score = max(0, 100 - (len(issues) * 15))
            confidence_level = "high" if score >= 85 else "medium" if score >= 70 else "low"

    return {
        "rule_set_version": ver,
        "outcome": outcome,
        "is_match": is_clean_match,
        "match_score": score,
        "confidence_level": confidence_level,
        "overall_confidence_percent": overall_percent if overall_percent > 0 else score,
        "field_confidence": confidence,
        "confidence_levels": confidence_levels,
        "low_confidence_fields": low_fields,
        "discrepancies": _json_safe(issues),
        "summary": llm_summary or (
            "All procurement checks and calculations passed." if is_clean_match
            else f"Matching identified {len(issues)} discrepancy item(s)."
        ),
        "accepted_quantities": {str(k): str(v) for k, v in accepted_by_po_line.items()},
        "inputs_summary": {
            "invoice_id": str(getattr(invoice, "id", "")),
            "po_number": getattr(invoice, "po_number_raw", None) or (po.po_number if po else None),
            "vendor_id": str(invoice_vendor_id) if invoice_vendor_id else None,
            "invoice_number": getattr(invoice, "invoice_no", None),
            "currency": currency,
            "invoice_total": str(total) if total is not None else None,
            "invoice_subtotal": str(subtotal) if subtotal is not None else None,
            "invoice_tax": str(actual_tax) if actual_tax is not None else None,
            "field_confidence": confidence,
            "po_ids": [str(po.id)] if po else [],
            "receipt_ids": [str(r.id) for r in receipts],
            "po_currency": getattr(po, "currency", None) if po else None,
            "po_tax": str(expected_tax) if expected_tax is not None else None,
            "po_total": str(po.total_amount) if po and getattr(po, "total_amount", None) is not None else None,
            "invoice_lines": [
                {
                    "sku": getattr(l, "item_code", None),
                    "description": getattr(l, "description", None),
                    "quantity": str(getattr(l, "qty", None)),
                    "unit_price": str(getattr(l, "unit_price", None)),
                    "line_total": str(getattr(l, "line_amount", None)),
                    "field_confidence": getattr(l, "field_confidence", None) or {},
                }
                for l in lines
            ],
            "po_lines": [
                {
                    "sku": getattr(pl, "item_code", None),
                    "description": getattr(pl, "description", None),
                    "quantity": str(getattr(pl, "qty", None)),
                    "unit_price": str(getattr(pl, "unit_price", None)),
                }
                for pl in po_lines
            ],
            "accepted_quantity_by_po_line": {
                str(k): str(v) for k, v in accepted_by_po_line.items()
            },
        },
        "validation_report": {
            "status": "validation_complete",
            "checks_performed": checks_performed,
            "discrepancies_count": len(issues),
        },
    }


# ---------------------------------------------------------------------------
# Public Entrypoint: evaluate_invoice
# ---------------------------------------------------------------------------


def evaluate_invoice(db: Session, invoice: Invoice, owner_id=None) -> dict[str, Any]:
    """
    Evaluate an invoice using LLM-first semantic matching followed by strict deterministic Python validations.
    
    Architecture:
    1. Scoped Data Loading: Queries POs and Goods Receipts permitted for owner_id.
    2. LLM Semantic Proposal: Asks the LLM to propose likely relationships, interpret descriptions,
       and map lines. The LLM is explicitly forbidden from computing arithmetic.
    3. Python Reference Validation: All proposed IDs and relationships are validated against DB records.
    4. Deterministic Python Validations: All financial arithmetic, tolerances, and rule checks are run in Python.
    5. Final Status Enforcement: Python enforces ready_to_pay vs needs_review. The LLM cannot approve payment.
    """
    # Step 1: Call LLM matching service for semantic candidate relationships
    llm_proposal = None
    try:
        from src.services.llm_matching_services import llm_propose_matches
        llm_proposal = llm_propose_matches(db, invoice, owner_id=owner_id)
    except Exception as exc:
        logger.warning("LLM matching proposal failed, falling back to deterministic lookup: %s", exc)

    # Step 2: Validate LLM Proposals against database records within permitted scope
    matched_po: PurchaseOrder | None = None
    po_rows: list[PurchaseOrder] = []

    # Check if LLM proposed a PO
    if llm_proposal and llm_proposal.get("matched_po_id"):
        try:
            p_uuid = uuid.UUID(str(llm_proposal["matched_po_id"]))
            candidate_po = (
                db.query(PurchaseOrder)
                .filter(
                    PurchaseOrder.id == p_uuid,
                    PurchaseOrder.is_current.is_(True),
                    *_owner_scope(PurchaseOrder, owner_id),
                )
                .first()
            )
            if candidate_po is not None:
                matched_po = candidate_po
                po_rows = [matched_po]
        except Exception:
            pass

    # If LLM proposed by PO number or no ID match, validate against scoped POs
    if matched_po is None and llm_proposal and llm_proposal.get("matched_po_number"):
        norm_num = normalize_value(llm_proposal["matched_po_number"])
        if norm_num:
            po_rows = [
                p for p in db.query(PurchaseOrder)
                .filter(PurchaseOrder.is_current.is_(True), *_owner_scope(PurchaseOrder, owner_id))
                .all()
                if normalize_value(p.po_number) == norm_num
            ]
            if len(po_rows) == 1:
                matched_po = po_rows[0]

    # Anti-hallucination check for PO proposal:
    # If the invoice explicitly specified po_number_raw, check whether it matches a real PO.
    # An LLM proposal must not arbitrarily switch the PO away from an explicitly stated valid PO.
    raw_po_ref = normalize_value(getattr(invoice, "po_number_raw", None))
    if raw_po_ref and matched_po is not None and normalize_value(matched_po.po_number) != raw_po_ref:
        exact_matches = [
            p for p in db.query(PurchaseOrder)
            .filter(PurchaseOrder.is_current.is_(True), *_owner_scope(PurchaseOrder, owner_id))
            .all()
            if normalize_value(p.po_number) == raw_po_ref
        ]
        if len(exact_matches) == 1:
            logger.warning(
                "Rejecting LLM proposed PO '%s': invoice explicitly references '%s' which exists in DB",
                matched_po.po_number, raw_po_ref
            )
            matched_po = exact_matches[0]
            po_rows = [matched_po]
        elif len(exact_matches) > 1:
            po_rows = exact_matches
            matched_po = None

    # If LLM didn't match or was unavailable, resolve from po_id or raw reference
    if matched_po is None and not po_rows:
        po_id = getattr(invoice, "po_id", None)
        if po_id:
            try:
                candidate_po = (
                    db.query(PurchaseOrder)
                    .filter(
                        PurchaseOrder.id == po_id,
                        PurchaseOrder.is_current.is_(True),
                        *_owner_scope(PurchaseOrder, owner_id),
                    )
                    .first()
                )
                if candidate_po is not None:
                    matched_po = candidate_po
                    po_rows = [matched_po]
            except Exception:
                pass

    if matched_po is None and not po_rows:
        po_ref = normalize_value(getattr(invoice, "po_number_raw", None))
        if po_ref:
            po_rows = [
                p for p in db.query(PurchaseOrder)
                .filter(PurchaseOrder.is_current.is_(True), *_owner_scope(PurchaseOrder, owner_id))
                .all()
                if normalize_value(p.po_number) == po_ref
            ]
            if len(po_rows) == 1:
                matched_po = po_rows[0]

    # Check for ambiguous POs
    if len(po_rows) > 1:
        lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
        issues = [
            _issue(
                "AMBIGUOUS_PO",
                "More than one purchase order matches this number.",
                actual=getattr(invoice, "po_number_raw", None),
                expected=len(po_rows),
                action="Resolve the duplicate PO records.",
            )
        ]
        return {
            "rule_set_version": RULE_SET_VERSION,
            "outcome": "needs_review",
            "is_match": False,
            "match_score": 50,
            "confidence_level": "medium",
            "overall_confidence_percent": 50,
            "field_confidence": getattr(invoice, "field_confidence", None) or {},
            "confidence_levels": {},
            "low_confidence_fields": [],
            "discrepancies": _json_safe(issues),
            "summary": "Ambiguous purchase order reference.",
            "inputs_summary": {
                "invoice_id": str(getattr(invoice, "id", "")),
                "po_number": getattr(invoice, "po_number_raw", None),
                "vendor_id": str(getattr(invoice, "vendor_id", None)) if getattr(invoice, "vendor_id", None) else None,
                "invoice_number": getattr(invoice, "invoice_no", None),
                "currency": getattr(invoice, "currency", None),
                "invoice_total": str(getattr(invoice, "total_amount", None)),
                "invoice_subtotal": str(getattr(invoice, "subtotal", None)),
                "invoice_tax": str(getattr(invoice, "tax_amount", None)),
                "po_ids": [str(p.id) for p in po_rows],
                "receipt_ids": [],
                "po_currency": None,
                "po_tax": None,
                "po_total": None,
                "invoice_lines": [
                    {
                        "sku": getattr(l, "item_code", None),
                        "description": getattr(l, "description", None),
                        "quantity": str(getattr(l, "qty", None)),
                        "unit_price": str(getattr(l, "unit_price", None)),
                        "line_total": str(getattr(l, "line_amount", None)),
                    }
                    for l in lines
                ],
                "po_lines": [],
                "accepted_quantity_by_po_line": {},
            },
        }

    # Auto-link PO and Vendor on invoice if validated
    if matched_po is not None:
        if getattr(invoice, "po_id", None) is None:
            invoice.po_id = matched_po.id
        if getattr(matched_po, "po_number", None) and not getattr(invoice, "po_number_raw", None):
            invoice.po_number_raw = matched_po.po_number

        # Auto-resolve / link vendor
        po_vendor = getattr(matched_po, "vendor", None)
        vendor_name = (
            (llm_proposal.get("resolved_vendor_name") if llm_proposal else None)
            or getattr(invoice, "vendor_name_raw", None)
            or (po_vendor.name if po_vendor else None)
        )
        if vendor_name and getattr(invoice, "vendor_id", None) is None:
            v_norm = vendor_name.strip()
            existing_v = db.query(Vendor).filter(func.lower(Vendor.name) == v_norm.lower()).first()
            if existing_v:
                invoice.vendor_id = existing_v.id
            elif getattr(matched_po, "vendor_id", None):
                invoice.vendor_id = matched_po.vendor_id

    # Step 3: Load scoped Lines, PO lines, and Goods Receipts
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == getattr(invoice, "id", None)).all()
    po_lines = (
        matched_po.lines if (matched_po and getattr(matched_po, "lines", None) is not None)
        else (db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == matched_po.id).all() if matched_po else [])
    )
    receipts = (
        db.query(GoodsReceipt)
        .filter(GoodsReceipt.po_id == matched_po.id, *_owner_scope(GoodsReceipt, owner_id))
        .all()
        if matched_po
        else []
    )
    receipt_lines = (
        db.query(GoodsReceiptLine)
        .filter(GoodsReceiptLine.goods_receipt_id.in_([g.id for g in receipts]))
        .all()
        if receipts
        else []
    )

    line_mappings = llm_proposal.get("line_mappings") if llm_proposal else []
    llm_discrepancies = llm_proposal.get("discrepancies") if llm_proposal else []
    llm_summary = llm_proposal.get("summary", "") if llm_proposal else ""

    # Step 4: Run pure deterministic calculations and business rule validation
    return execute_deterministic_matching(
        db=db,
        invoice=invoice,
        lines=lines,
        po=matched_po,
        po_lines=po_lines,
        receipts=receipts,
        receipt_lines=receipt_lines,
        line_mappings=line_mappings,
        owner_id=owner_id,
        llm_discrepancies=llm_discrepancies,
        llm_summary=llm_summary,
        rule_set_version=RULE_SET_VERSION,
    )
