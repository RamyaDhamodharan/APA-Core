"""
Core service for multi-signal field-level confidence scoring and explanation generation.
Combines:
1. OCR quality (0.35)
2. Two-pass consistency (0.25)
3. Cross-field math checks (0.20)
4. Master-data validity (0.10)
5. Format validation (0.10)
Applies hard caps and produces human-readable explanations for low scores.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import logging
import re
from typing import Any, Optional
from sqlalchemy.orm import Session

from ..config.confidence_config import CONFIDENCE_CONFIG
from ..models.purchase_order_models import PurchaseOrder
from ..models.vendor_models import Vendor
from ..schemas.confidence_schemas import InvoiceConfidenceBreakdown, ScoredField, SignalBreakdown

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        cleaned = re.sub(r"[^\d.-]", "", str(value))
        if not cleaned or cleaned == "-":
            return None
        return Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _normalize_str(val: Any) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val).strip().casefold())


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    if isinstance(val, (date, datetime)):
        return val.date() if isinstance(val, datetime) else val
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def calculate_field_confidence(
    field_name: str,
    value: Any,
    *,
    ocr_score: float,
    consistency_score: float,
    math_score: float,
    master_data_score: float,
    format_score: float,
    reasons: list[str],
    is_blurry: bool = False,
    caps: Optional[dict[str, float]] = None,
) -> ScoredField:
    """Calculate weighted composite score and apply hard caps."""
    cfg = CONFIDENCE_CONFIG
    caps = caps or {}

    weighted = (
        (ocr_score * cfg.weight_ocr)
        + (consistency_score * cfg.weight_consistency)
        + (math_score * cfg.weight_math)
        + (master_data_score * cfg.weight_master_data)
        + (format_score * cfg.weight_format)
    )

    if is_blurry:
        weighted = max(0.0, weighted - cfg.blurry_image_penalty)
        if "Image is blurry or low quality; overall readability reduced" not in reasons:
            reasons.append("Image is blurry or low quality; overall readability reduced")

    # Apply hard caps (cannot be averaged away)
    final_score = weighted
    for cap_reason, cap_val in caps.items():
        if final_score > cap_val:
            final_score = cap_val
            if cap_reason not in reasons:
                reasons.append(cap_reason)

    score_int = max(0, min(100, int(round(final_score))))

    return ScoredField(
        value=value,
        confidence=score_int,
        source_signals=SignalBreakdown(
            ocr=round(ocr_score, 1),
            consistency=round(consistency_score, 1),
            math=round(math_score, 1),
            master_data=round(master_data_score, 1),
            format=round(format_score, 1),
            reasons=reasons,
        ),
        original_value=value,
    )


def score_invoice_extraction(
    db: Optional[Session],
    primary_data: dict[str, Any],
    secondary_data: Optional[dict[str, Any]] = None,
    *,
    ocr_quality: float = 95.0,
    is_blurry: bool = False,
) -> InvoiceConfidenceBreakdown:
    """
    Evaluate all extracted fields of a bill and compute field-level confidence scores.
    """
    cfg = CONFIDENCE_CONFIG
    secondary = secondary_data or {}
    scored_fields: dict[str, ScoredField] = {}
    line_item_scores: list[dict[str, ScoredField]] = []

    # -------------------------------------------------------------
    # 1. Cross-field Math Validation
    # -------------------------------------------------------------
    subtotal = _to_decimal(primary_data.get("subtotal"))
    tax = _to_decimal(primary_data.get("tax"))
    total = _to_decimal(primary_data.get("total") or primary_data.get("amount"))
    lines = primary_data.get("line_items") or []

    math_header_ok = True
    math_header_reasons: list[str] = []

    if subtotal is not None and tax is not None and total is not None:
        expected_total = subtotal + tax
        if abs(expected_total - total) > Decimal("0.02"):
            math_header_ok = False
            math_header_reasons.append(
                f"Total ({total}) does not equal subtotal ({subtotal}) + tax ({tax})"
            )
    elif total is not None and subtotal is None and tax is None:
        # Single total provided with no tax breakdown
        math_header_ok = True
    elif total is None:
        math_header_ok = False
        math_header_reasons.append("Invoice total amount is missing")

    line_sum = Decimal("0")
    line_math_ok = True
    line_math_reasons: list[str] = []

    for idx, item in enumerate(lines):
        if not isinstance(item, dict):
            continue
        qty = _to_decimal(item.get("qty") or item.get("quantity"))
        unit_price = _to_decimal(item.get("unit_price"))
        amount = _to_decimal(item.get("line_amount") or item.get("amount"))

        if amount is not None:
            line_sum += amount
        if qty is not None and unit_price is not None and amount is not None:
            expected_line = round(qty * unit_price, 2)
            if abs(expected_line - amount) > Decimal("0.02"):
                line_math_ok = False
                line_math_reasons.append(
                    f"Line {idx+1} amount ({amount}) does not equal qty ({qty}) * unit price ({unit_price})"
                )

    if subtotal is not None and lines and abs(line_sum - subtotal) > Decimal("0.02"):
        math_header_ok = False
        math_header_reasons.append(
            f"Sum of line items ({line_sum}) does not equal invoice subtotal ({subtotal})"
        )

    # -------------------------------------------------------------
    # 2. Master Data Lookups
    # -------------------------------------------------------------
    vendor_name = str(primary_data.get("vendor") or "").strip()
    vendor_exists = False
    if db and vendor_name:
        from ..services.vendor_resolution_services import resolve_vendor
        matched_vendor = resolve_vendor(db, vendor_name, tax_id=primary_data.get("vendor_tax_id"))
        vendor_exists = matched_vendor is not None
    elif vendor_name:
        # Fallback when DB is not injected
        vendor_exists = len(vendor_name) >= 3

    po_number = str(primary_data.get("order_number") or primary_data.get("po_number") or "").strip()
    po_exists = True
    if db and po_number:
        po_record = db.query(PurchaseOrder).filter(
            PurchaseOrder.po_number.ilike(po_number),
            PurchaseOrder.is_current.is_(True),
        ).first()
        po_exists = po_record is not None

    gl_code = str(primary_data.get("gl_code") or "").strip()
    gl_valid = gl_code in cfg.valid_gl_codes if gl_code else False

    # -------------------------------------------------------------
    # 3. Two-Pass Consistency Helper
    # -------------------------------------------------------------
    def consistency(key: str, val1: Any) -> tuple[float, list[str]]:
        if not secondary:
            return (100.0, [])  # Single pass fallback
        val2 = secondary.get(key)
        if val1 is None and val2 is None:
            return (100.0, [])
        if val1 is None or val2 is None:
            return (20.0, [f"Second extraction did not find {key}"])
        s1 = _normalize_str(val1)
        s2 = _normalize_str(val2)
        if s1 == s2:
            return (100.0, [])
        if s1 in s2 or s2 in s1:
            return (70.0, [f"Second extraction produced slight variation: '{val2}'"])
        return (20.0, [f"Extraction mismatch: pass 1 was '{val1}', pass 2 was '{val2}'"])

    # -------------------------------------------------------------
    # 4. Score Header Fields
    # -------------------------------------------------------------
    # Field: vendor
    c_score, c_reasons = consistency("vendor", vendor_name)
    v_reasons = list(c_reasons)
    v_caps = {}
    v_format = 100.0 if len(vendor_name) >= 2 else 0.0
    if v_format == 0:
        v_caps["Vendor name is blank or invalid format"] = cfg.cap_format_invalid
    v_master = 100.0 if vendor_exists else 0.0
    if not vendor_exists:
        v_caps["Vendor not in approved vendor list"] = cfg.cap_vendor_missing
        v_reasons.append("Vendor not found in approved master vendor list")

    scored_fields["vendor"] = calculate_field_confidence(
        "vendor",
        vendor_name,
        ocr_score=ocr_quality,
        consistency_score=c_score,
        math_score=100.0,
        master_data_score=v_master,
        format_score=v_format,
        reasons=v_reasons,
        is_blurry=is_blurry,
        caps=v_caps,
    )

    # Field: invoice_number
    inv_num = str(primary_data.get("invoice_number") or "").strip()
    c_score, c_reasons = consistency("invoice_number", inv_num)
    inv_reasons = list(c_reasons)
    inv_caps = {}
    inv_format = 100.0 if (inv_num and len(inv_num) >= 3 and not inv_num.lower().startswith("temp")) else 0.0
    if inv_format == 0:
        inv_caps["Invoice number is missing or invalid format"] = cfg.cap_format_invalid
        inv_reasons.append("Invoice number is missing or invalid format")

    scored_fields["invoice_number"] = calculate_field_confidence(
        "invoice_number",
        inv_num,
        ocr_score=ocr_quality,
        consistency_score=c_score,
        math_score=100.0,
        master_data_score=100.0,
        format_score=inv_format,
        reasons=inv_reasons,
        is_blurry=is_blurry,
        caps=inv_caps,
    )

    # Field: amount (total)
    tot_val = primary_data.get("total") or primary_data.get("amount")
    c_score, c_reasons = consistency("total", tot_val)
    tot_reasons = list(c_reasons) + list(math_header_reasons)
    tot_caps = {}
    tot_format = 100.0 if total is not None and total > 0 else 0.0
    if tot_format == 0:
        tot_caps["Total amount is non-numeric or zero"] = cfg.cap_format_invalid
    if not math_header_ok:
        tot_caps["Cross-field math check failed"] = cfg.cap_math_fail

    scored_fields["amount"] = calculate_field_confidence(
        "amount",
        float(total) if total is not None else tot_val,
        ocr_score=ocr_quality,
        consistency_score=c_score,
        math_score=100.0 if math_header_ok else 0.0,
        master_data_score=100.0,
        format_score=tot_format,
        reasons=tot_reasons,
        is_blurry=is_blurry,
        caps=tot_caps,
    )
    # Also link as 'total' for backward compatibility
    scored_fields["total"] = scored_fields["amount"]

    # Field: due_date
    raw_due = primary_data.get("due_date")
    parsed_due = _parse_date(raw_due)
    c_score, c_reasons = consistency("due_date", raw_due)
    due_reasons = list(c_reasons)
    due_caps = {}
    due_format = 100.0 if parsed_due is not None else 0.0
    if due_format == 0:
        due_caps["Due date is missing or unparseable"] = cfg.cap_format_invalid
        due_reasons.append("Due date could not be parsed as a valid calendar date")

    scored_fields["due_date"] = calculate_field_confidence(
        "due_date",
        parsed_due.isoformat() if parsed_due else raw_due,
        ocr_score=ocr_quality,
        consistency_score=c_score,
        math_score=100.0,
        master_data_score=100.0,
        format_score=due_format,
        reasons=due_reasons,
        is_blurry=is_blurry,
        caps=due_caps,
    )

    # Field: gl_code
    c_score, c_reasons = consistency("gl_code", gl_code)
    gl_reasons = list(c_reasons)
    gl_caps = {}
    gl_master = 100.0 if gl_valid else 0.0
    if not gl_valid:
        gl_caps["GL code not recognized in Chart of Accounts"] = cfg.cap_vendor_missing
        gl_reasons.append(f"GL code '{gl_code or 'MISSING'}' is not recognized in Chart of Accounts")

    scored_fields["gl_code"] = calculate_field_confidence(
        "gl_code",
        gl_code,
        ocr_score=ocr_quality,
        consistency_score=c_score,
        math_score=100.0,
        master_data_score=gl_master,
        format_score=100.0 if gl_code else 0.0,
        reasons=gl_reasons,
        is_blurry=is_blurry,
        caps=gl_caps,
    )

    # Field: tax
    c_score, c_reasons = consistency("tax", primary_data.get("tax"))
    tax_reasons = list(c_reasons)
    tax_caps = {}
    tax_format = 100.0 if tax is not None and tax >= 0 else 0.0
    if not math_header_ok and tax is not None:
        tax_caps["Cross-field math check failed"] = cfg.cap_math_fail
        tax_reasons.extend(math_header_reasons)

    scored_fields["tax"] = calculate_field_confidence(
        "tax",
        float(tax) if tax is not None else 0.0,
        ocr_score=ocr_quality,
        consistency_score=c_score,
        math_score=100.0 if math_header_ok else 0.0,
        master_data_score=100.0,
        format_score=tax_format,
        reasons=tax_reasons,
        is_blurry=is_blurry,
        caps=tax_caps,
    )

    # -------------------------------------------------------------
    # 5. Score Line Items
    # -------------------------------------------------------------
    for idx, item in enumerate(lines):
        if not isinstance(item, dict):
            continue
        line_desc = str(item.get("description") or "").strip()
        qty = _to_decimal(item.get("qty") or item.get("quantity"))
        unit_price = _to_decimal(item.get("unit_price"))
        amount = _to_decimal(item.get("line_amount") or item.get("amount"))

        # Line math check
        l_math_ok = True
        l_math_reasons = []
        if qty is not None and unit_price is not None and amount is not None:
            if abs(round(qty * unit_price, 2) - amount) > Decimal("0.02"):
                l_math_ok = False
                l_math_reasons.append(
                    f"Line qty ({qty}) * unit price ({unit_price}) does not equal line amount ({amount})"
                )

        # Scored Description
        scored_desc = calculate_field_confidence(
            "description",
            line_desc,
            ocr_score=ocr_quality,
            consistency_score=100.0,
            math_score=100.0,
            master_data_score=100.0,
            format_score=100.0 if len(line_desc) >= 2 else 0.0,
            reasons=["Description is missing or empty"] if len(line_desc) < 2 else [],
            is_blurry=is_blurry,
            caps={"Description missing": cfg.cap_format_invalid} if len(line_desc) < 2 else {},
        )

        # Scored Qty
        q_format = 100.0 if qty is not None and qty > 0 else 0.0
        q_caps = {}
        if q_format == 0:
            q_caps["Quantity must be a positive number"] = cfg.cap_format_invalid
        scored_qty = calculate_field_confidence(
            "qty",
            float(qty) if qty is not None else None,
            ocr_score=ocr_quality,
            consistency_score=100.0,
            math_score=100.0 if l_math_ok else 0.0,
            master_data_score=100.0,
            format_score=q_format,
            reasons=list(l_math_reasons),
            is_blurry=is_blurry,
            caps=q_caps,
        )

        # Scored Unit Price
        p_format = 100.0 if unit_price is not None and unit_price > 0 else 0.0
        p_caps = {}
        if p_format == 0:
            p_caps["Unit price must be a positive number"] = cfg.cap_format_invalid
        if not l_math_ok:
            p_caps["Line math mismatch"] = cfg.cap_math_fail

        scored_price = calculate_field_confidence(
            "unit_price",
            float(unit_price) if unit_price is not None else None,
            ocr_score=ocr_quality,
            consistency_score=100.0,
            math_score=100.0 if l_math_ok else 0.0,
            master_data_score=100.0,
            format_score=p_format,
            reasons=list(l_math_reasons),
            is_blurry=is_blurry,
            caps=p_caps,
        )

        line_item_scores.append({
            "description": scored_desc,
            "qty": scored_qty,
            "unit_price": scored_price,
        })

    # -------------------------------------------------------------
    # 6. Overall Confidence & Gating Determination
    # -------------------------------------------------------------
    all_scores = [f.confidence for f in scored_fields.values()]
    for item in line_item_scores:
        all_scores.extend(f.confidence for f in item.values())

    min_score = min(all_scores) if all_scores else 0
    avg_score = int(round(sum(all_scores) / len(all_scores))) if all_scores else 0

    # Find lowest field for user display
    lowest_field = ""
    lowest_val = 101
    for name, f in scored_fields.items():
        if f.confidence < lowest_val:
            lowest_val = f.confidence
            lowest_field = name

    # Gating rule: ANY field below threshold forces Human Verification
    requires_human_verification = any(s < cfg.threshold for s in all_scores)

    return InvoiceConfidenceBreakdown(
        overall_confidence=min_score,  # Lowest score represents the bill's gating confidence
        requires_verification=requires_human_verification,
        lowest_field=lowest_field,
        fields=scored_fields,
        line_items=line_item_scores,
    )
