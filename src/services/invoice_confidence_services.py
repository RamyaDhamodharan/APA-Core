"""
Application module for extraction confidence and verification gating.
Flow: normalize raw OCR/LLM confidence scores, evaluate thresholds, and enforce human verification gating.
Called by: ap_decision_engine, document_workflow_services, invoice_issue_services.
Calls: none directly.
Tables: invoices, invoice_lines (confidence jsonb fields).
"""
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# Material invoice fields evaluated for confidence scoring
INVOICE_CONFIDENCE_FIELDS = (
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

# Mandatory core fields that must have high confidence to bypass manual verification
CORE_EXTRACTION_FIELDS = (
    "vendor",
    "invoice_number",
    "invoice_date",
    "total",
)

# Line-item level fields evaluated for arithmetic and pricing confidence
LINE_CONFIDENCE_FIELDS = ("qty", "unit_price", "line_amount")


def overall_confidence_percent(confidence: dict[str, Any] | None) -> int:
    """
    Calculate the overall invoice confidence percentage (0-100%).
    Inputs: confidence (dict[str, Any] | None) - dictionary of field-level confidence scores.
    Output: int - aggregate confidence percentage from 0 to 100.
    Effects: pure function, no database operations.
    Results: used for dashboard confidence pills and verification gating.
    """
    confidence = confidence if isinstance(confidence, dict) else {}

    # Step 1: Check if an explicit overall match or LLM confidence was provided
    for key in ("overall_match_confidence", "overall_confidence"):
        if key in confidence:
            val = confidence[key]
            raw = val.get("confidence", val.get("score")) if isinstance(val, dict) else val
            try:
                score_num = Decimal(str(raw))
                # Scale 0.0-1.0 float scores to 0-100%
                if Decimal("0") <= score_num <= Decimal("1"):
                    score_num *= Decimal("100")
                return int(score_num.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            except Exception:
                pass

    def score(raw: Any) -> Decimal | None:
        """Helper to extract and scale a numerical confidence score."""
        if raw is None:
            return None
        value = (
            raw.get("confidence", raw.get("score"))
            if isinstance(raw, dict)
            else raw
        )
        try:
            result = Decimal(str(value))
            if Decimal("0") <= result <= Decimal("1"):
                result *= Decimal("100")
            return min(Decimal("100"), max(Decimal("0"), result))
        except Exception:
            return None

    # Step 2: Evaluate material fields (excluding internal ERP codes like gl_code)
    eval_fields = [f for f in INVOICE_CONFIDENCE_FIELDS if f not in ("gl_code", "order_number")]
    scores = []
    for field in eval_fields:
        s = score(confidence.get(field))
        if s is not None:
            scores.append(s)

    # Step 3: Include line-item level confidence scores
    lines = confidence.get("line_items")
    if isinstance(lines, list) and lines:
        for line in lines:
            if isinstance(line, dict):
                for lf in LINE_CONFIDENCE_FIELDS:
                    ls = score(line.get(lf))
                    if ls is not None:
                        scores.append(ls)

    # Step 4: If no scores are recorded, return 0% confidence
    if not scores:
        return 0

    # Step 5: Calculate arithmetic mean rounded to nearest integer
    overall = sum(scores, Decimal("0")) / Decimal(len(scores))
    return int(overall.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def confidence_threshold() -> float:
    """
    Return the minimum confidence threshold percentage from environment (default 85%).
    Inputs: none.
    Output: float - threshold between 0.0 and 100.0.
    Effects: reads FIELD_CONFIDENCE_MIN environment variable.
    Results: threshold used across matching and verification logic.
    """
    try:
        return min(100.0, max(0.0, float(os.getenv("FIELD_CONFIDENCE_MIN", "85"))))
    except ValueError:
        return 85.0


def _confidence_entry(value: Any, present: bool) -> dict[str, Any]:
    """Format a single field confidence entry with score, derived flag, and signal breakdown."""
    raw = value
    derived = False
    source_signals = None
    edited_by = None
    edited_at = None
    original_value = None
    if isinstance(value, dict):
        raw = value.get("confidence", value.get("score"))
        derived = bool(value.get("derived", False))
        source_signals = value.get("source_signals")
        edited_by = value.get("edited_by")
        edited_at = value.get("edited_at")
        original_value = value.get("original_value")
    try:
        score = float(raw)
        if 0 <= score <= 1:
            score *= 100
        score = min(100.0, max(0.0, score))
    except (TypeError, ValueError):
        score = 0.0
        derived = True
    entry = {"confidence": score, "derived": derived}
    if source_signals is not None:
        entry["source_signals"] = source_signals
    if edited_by is not None:
        entry["edited_by"] = edited_by
    if edited_at is not None:
        entry["edited_at"] = edited_at
    if original_value is not None:
        entry["original_value"] = original_value
    return entry


def normalize_invoice_confidence(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize raw confidence structures into standard format across all material fields.
    Inputs: data (dict[str, Any]) - invoice extraction dictionary.
    Output: dict[str, Any] - normalized dictionary mapping field names to confidence objects.
    Effects: pure function, no database operations.
    Results: consistent schema for frontend inspection and database persistence.
    """
    raw = data.get("field_confidence") or data.get("confidence") or {}
    raw = raw if isinstance(raw, dict) else {}
    # Step 1: Normalize header-level confidence entries
    normalized = {
        field: _confidence_entry(raw.get(field), data.get(field) is not None)
        for field in INVOICE_CONFIDENCE_FIELDS
    }
    # Step 2: Normalize line-item level confidence entries
    lines = data.get("line_items")
    normalized["line_items"] = [
        {
            field: _confidence_entry(
                (line.get("field_confidence") or {}).get(field)
                if isinstance(line.get("field_confidence"), dict)
                else None,
                line.get(field) is not None,
            )
            for field in LINE_CONFIDENCE_FIELDS
        }
        for line in lines or []
        if isinstance(line, dict)
    ]
    return normalized


def requires_verification(confidence: dict[str, Any]) -> bool:
    """
    Determine if an invoice must be held in 'verifying' state for human data review.
    Inputs: confidence (dict[str, Any]) - normalized field confidence dictionary.
    Output: bool - True if manual verification is required, False if high-confidence.
    Effects: pure function.
    Results: gates transition between received and matching states.
    """
    # Step 1: Missing confidence dictionary always forces manual verification
    if not confidence:
        return True

    # Step 2: Direct high-confidence LLM matching bypasses field-level gating
    for key in ("overall_match_confidence", "overall_confidence"):
        if key in confidence:
            val = confidence[key]
            s = float(val.get("confidence", 0) if isinstance(val, dict) else val)
            if s >= confidence_threshold():
                return False

    threshold = confidence_threshold()
    present_fields = {
        field for field in CORE_EXTRACTION_FIELDS if field in confidence
    }
    # Step 3: Require at least core header fields to be present
    if len(present_fields) < 2:
        return True

    # Step 4: Check if any core header field falls below the confidence threshold
    header_low = any(
        entry["confidence"] < threshold
        for field, entry in confidence.items()
        if field in CORE_EXTRACTION_FIELDS and isinstance(entry, dict)
    )
    if header_low:
        return True

    # Step 5: Check if any line item price/qty falls below the confidence threshold
    line_low = any(
        entry[field]["confidence"] < threshold
        for entry in confidence.get("line_items", [])
        for field in LINE_CONFIDENCE_FIELDS
        if isinstance(entry, dict) and field in entry and isinstance(entry[field], dict)
    )
    return line_low


def confidence_unavailable(confidence: dict[str, Any] | None) -> bool:
    """
    Check if confidence scores are missing or uncomputed for essential core fields.
    Inputs: confidence (dict[str, Any] | None).
    Output: bool - True if confidence metadata is unavailable.
    Effects: pure function.
    Results: indicates whether the OCR/LLM model failed to provide confidence metrics.
    """
    if not confidence or any(
        field not in confidence for field in CORE_EXTRACTION_FIELDS
    ):
        return True
    if any(
        not isinstance(confidence.get(field), dict)
        or confidence[field].get("confidence") is None
        for field in CORE_EXTRACTION_FIELDS
    ):
        return True
    return False


def confidence_level(confidence: dict[str, Any] | None) -> str:
    """
    Return a categorical display-safe confidence label ('high', 'medium', 'low', 'unknown').
    Inputs: confidence (dict[str, Any] | None).
    Output: str - 'high' (>=90%), 'medium' (>=75%), 'low' (<75%), or 'unknown'.
    Effects: pure function.
    Results: drives UI badges without exposing raw internal floating-point numbers.
    """
    if not confidence:
        return "unknown"
    if any(field not in confidence for field in INVOICE_CONFIDENCE_FIELDS):
        return "unknown"
    entries = [confidence[field] for field in INVOICE_CONFIDENCE_FIELDS]
    for line in confidence.get("line_items", []):
        if any(field not in line for field in LINE_CONFIDENCE_FIELDS):
            return "unknown"
        entries.extend(line[field] for field in LINE_CONFIDENCE_FIELDS)
    if any(
        not isinstance(entry, dict)
        or entry.get("confidence") is None
        or entry.get("derived", False)
        for entry in entries
    ):
        return "unknown"
    scores = [float(entry["confidence"]) for entry in entries]
    lowest = min(scores)
    # Lowest score across all material fields determines overall tier
    if lowest >= 90:
        return "high"
    if lowest >= 75:
        return "medium"
    return "low"
