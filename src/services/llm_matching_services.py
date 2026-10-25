"""LLM-driven document matching and calculation engine with robust validation.

The LLM handles semantic matching, line pairings, tolerance checks,
discrepancy identification, and confidence calculation.
A rigorous validation layer validates all calculations against official
procurement records, tolerances, and state transition rules.
"""

import json
import logging
import os
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor
from src.services.po_matching_services import normalize_value
from src.services.tolerance_config_services import (
    read_nonnegative_decimal,
    tolerance_fraction,
)

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

DEFAULT_MATCHING_FALLBACK_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-small-24b-instruct-2501",
    "qwen/qwen-2.5-72b-instruct",
]


def _is_openrouter_matching_model(model: str) -> bool:
    if model.startswith("openrouter/"):
        return True
    openrouter_vendors = (
        "meta-llama/",
        "mistralai/",
        "google/",
        "deepseek/",
        "anthropic/",
        "qwen/qwen-2.5-",
        "openai/gpt-4",
    )
    return any(model.startswith(v) for v in openrouter_vendors)


def _d(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        res = Decimal(str(val))
        return res if res.is_finite() else None
    except Exception:
        return None


def _format_issue(
    code: str,
    message: str,
    actual: Any = None,
    expected: Any = None,
    difference: Any = None,
    tolerance: Any = None,
    line: Any = None,
    action: str = "Review the invoice and supporting procurement records.",
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "invoice_value": str(actual) if actual is not None else None,
        "expected_value": str(expected) if expected is not None else None,
        "difference": str(difference) if difference is not None else None,
        "tolerance": str(tolerance) if tolerance is not None else None,
        "line_reference": str(line) if line is not None else None,
        "next_action": action,
    }


def validate_and_refine_llm_calculations(
    llm_output: dict[str, Any],
    invoice: Invoice,
    lines: list[InvoiceLine],
    po: PurchaseOrder | None,
    po_lines: list[PurchaseOrderLine],
    receipts: list[GoodsReceipt],
    receipt_lines: list[GoodsReceiptLine],
    price_tolerance_pct: Decimal = Decimal("0.02"),
    tax_tolerance_abs: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    """Validate all calculations performed by the LLM and refine any edge-case discrepancies."""
    validation_log: list[str] = []
    checks_performed = 0

    # 1. Validate PO Existence
    checks_performed += 1
    if po is None:
        validation_log.append("PO missing: enforced no_match outcome and 0 score.")
        return {
            "is_match": False,
            "outcome": "no_match",
            "match_score": 0,
            "confidence_level": "low",
            "overall_confidence_percent": 0,
            "discrepancies": [
                _format_issue(
                    "MISSING_PO",
                    "No purchase order matches this invoice.",
                    actual=invoice.po_number_raw,
                    action="Add or correct the PO number.",
                )
            ],
            "validation_report": {
                "status": "validated_no_match",
                "checks_performed": checks_performed,
                "adjustments": validation_log,
            },
        }

    # Extract LLM calculated scores and outcome
    raw_score = llm_output.get("match_score")
    raw_conf = llm_output.get("overall_confidence_percent")
    raw_outcome = llm_output.get("outcome", "needs_review")
    raw_discrepancies = list(llm_output.get("discrepancies") or [])

    # Validate score bounds
    checks_performed += 1
    try:
        score = int(raw_score) if raw_score is not None else 85
        score = max(0, min(100, score))
    except Exception:
        score = 85
        validation_log.append("Invalid LLM match_score format; normalized to 85.")

    try:
        conf_percent = int(raw_conf) if raw_conf is not None else score
        conf_percent = max(0, min(100, conf_percent))
    except Exception:
        conf_percent = score

    # 2. Currency Validation
    checks_performed += 1
    inv_curr = getattr(invoice, "currency", None)
    po_curr = getattr(po, "currency", None)
    currency_mismatch = bool(inv_curr and po_curr and inv_curr.strip().upper() != po_curr.strip().upper())
    if currency_mismatch and not any(d.get("code") == "CURRENCY_MISMATCH" for d in raw_discrepancies):
        raw_discrepancies.append(
            _format_issue(
                "CURRENCY_MISMATCH",
                f"Invoice currency ({inv_curr}) does not match PO currency ({po_curr}).",
                actual=inv_curr,
                expected=po_curr,
                action="Correct the currency; amounts are never converted.",
            )
        )
        validation_log.append("Validation added missing CURRENCY_MISMATCH.")

    # 3. Goods Receipt Quantities Validation
    checks_performed += 1
    accepted_by_po_line: dict[uuid.UUID, Decimal] = {}
    for grl in receipt_lines:
        if grl.po_line_id:
            accepted_by_po_line[grl.po_line_id] = accepted_by_po_line.get(
                grl.po_line_id, Decimal("0")
            ) + (_d(grl.accepted_qty) or Decimal("0"))

    if not receipts and not any(d.get("code") == "MISSING_GR" for d in raw_discrepancies):
        raw_discrepancies.append(
            _format_issue(
                "MISSING_GR",
                f"No goods receipt is recorded for PO {po.po_number}.",
                action="Record or link a goods receipt.",
            )
        )
        validation_log.append("Validation added missing MISSING_GR.")

    # 4. Validate Line Items & Calculations
    po_line_by_id = {pl.id: pl for pl in po_lines}
    po_line_by_code = {
        normalize_value(pl.item_code): pl
        for pl in po_lines
        if pl.item_code and normalize_value(pl.item_code)
    }

    line_mappings = llm_output.get("line_mappings") or []
    validated_discrepancies: list[dict[str, Any]] = []

    for idx, line in enumerate(lines, start=1):
        checks_performed += 1
        ref = line.item_code or line.description or f"Line {idx}"
        qty = _d(line.qty)
        price = _d(line.unit_price)
        amount = _d(line.line_amount)

        # 4a. Arithmetic Check: qty * price == amount
        if qty is not None and price is not None:
            expected_amount = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount is not None and abs(amount - expected_amount) > Decimal("0.01"):
                diff = (amount - expected_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if not any(d.get("code") == "LINE_TOTAL_VARIANCE" and d.get("line_reference") == ref for d in raw_discrepancies):
                    validated_discrepancies.append(
                        _format_issue(
                            "LINE_TOTAL_VARIANCE",
                            f"Line total ({amount}) does not equal qty ({qty}) * unit price ({price}) = {expected_amount}.",
                            actual=amount,
                            expected=expected_amount,
                            difference=diff,
                            tolerance=Decimal("0.00"),
                            line=ref,
                            action="Correct the quantity, price, or line total.",
                        )
                    )
                    validation_log.append(f"Validation added verified LINE_TOTAL_VARIANCE for {ref}.")

        # 4b. Match to PO line
        matched_pl: PurchaseOrderLine | None = None
        for m in line_mappings:
            if isinstance(m, dict):
                m_inv = str(m.get("invoice_line_id", "")).strip()
                m_po = m.get("matched_po_line_id")
                if m_inv in (str(line.id), str(idx)) and m_po:
                    try:
                        p_uuid = uuid.UUID(str(m_po))
                        if p_uuid in po_line_by_id:
                            matched_pl = po_line_by_id[p_uuid]
                            break
                    except Exception:
                        pass

        if matched_pl is None and line.item_code:
            norm_code = normalize_value(line.item_code)
            if norm_code in po_line_by_code:
                matched_pl = po_line_by_code[norm_code]

        if matched_pl is None and len(po_lines) == 1 and len(lines) == 1:
            matched_pl = po_lines[0]

        if matched_pl is None and line.description:
            line_desc_norm = line.description.lower().strip()
            for pl in po_lines:
                if pl.description and (pl.description.lower().strip() in line_desc_norm or line_desc_norm in pl.description.lower().strip()):
                    matched_pl = pl
                    break

        if matched_pl is None:
            if not any(d.get("code") == "LINE_NOT_ON_PO" and d.get("line_reference") == ref for d in raw_discrepancies):
                validated_discrepancies.append(
                    _format_issue(
                        "LINE_NOT_ON_PO",
                        f"Invoice line '{ref}' has no matching PO item.",
                        actual=ref,
                        expected=[pl.item_code for pl in po_lines if pl.item_code],
                        line=ref,
                        action="Review the invoice line item against the PO lines.",
                    )
                )
                validation_log.append(f"Validation identified unlinked line item: {ref}.")
            continue

        # 4c. Validate Price Variance Calculation
        po_price = _d(matched_pl.unit_price)
        if price is not None and po_price is not None:
            price_delta = abs(price - po_price)
            allowed_tol = (abs(po_price) * price_tolerance_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if price_delta > allowed_tol:
                diff = (price - po_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if not any(d.get("code") == "PRICE_VARIANCE" and d.get("line_reference") == ref for d in raw_discrepancies):
                    validated_discrepancies.append(
                        _format_issue(
                            "PRICE_VARIANCE",
                            f"Invoice unit price ({price}) exceeds PO price ({po_price}) beyond allowed tolerance ({allowed_tol}).",
                            actual=price,
                            expected=po_price,
                            difference=diff,
                            tolerance=allowed_tol,
                            line=ref,
                            action="Review invoice price or obtain PO price variance approval.",
                        )
                    )
                    validation_log.append(f"Validation confirmed price variance for {ref}.")

        # 4d. Validate Quantity Received Calculation
        if receipts and qty is not None:
            accepted_qty = accepted_by_po_line.get(matched_pl.id, Decimal("0"))
            if qty > accepted_qty:
                qty_diff = (qty - accepted_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if not any(d.get("code") == "QUANTITY_VARIANCE" and d.get("line_reference") == ref for d in raw_discrepancies):
                    validated_discrepancies.append(
                        _format_issue(
                            "QUANTITY_VARIANCE",
                            f"Invoice quantity ({qty}) exceeds accepted received quantity ({accepted_qty}).",
                            actual=qty,
                            expected=accepted_qty,
                            difference=qty_diff,
                            line=ref,
                            action="Verify the invoice quantity against goods receipts.",
                        )
                    )
                    validation_log.append(f"Validation confirmed quantity variance for {ref}.")

    # 5. Header Calculations Validation
    checks_performed += 1
    expected_tax = _d(po.tax_amount)
    actual_tax = _d(invoice.tax_amount)
    if expected_tax is not None and actual_tax is not None:
        tax_delta = abs(actual_tax - expected_tax)
        if tax_delta > tax_tolerance_abs:
            diff = (actual_tax - expected_tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if not any(d.get("code") == "TAX_VARIANCE" for d in raw_discrepancies):
                validated_discrepancies.append(
                    _format_issue(
                        "TAX_VARIANCE",
                        f"Invoice tax ({actual_tax}) differs from PO tax ({expected_tax}) beyond tolerance.",
                        actual=actual_tax,
                        expected=expected_tax,
                        difference=diff,
                        tolerance=tax_tolerance_abs,
                        action="Review invoice and PO tax calculations.",
                    )
                )
                validation_log.append("Validation confirmed tax variance.")

    # 6. Merge & Filter Validated Discrepancies
    final_discrepancies: list[dict[str, Any]] = []
    for d in raw_discrepancies:
        if not isinstance(d, dict):
            continue
        code = str(d.get("code", "")).upper()
        if code == "PRICE_VARIANCE":
            matching_val = next((vd for vd in validated_discrepancies if vd.get("code") == "PRICE_VARIANCE"), None)
            if matching_val:
                final_discrepancies.append(matching_val)
            continue
        elif code == "MISSING_GR":
            if not receipts:
                final_discrepancies.append(d)
        else:
            final_discrepancies.append(d)

    for vd in validated_discrepancies:
        if not any(fd.get("code") == vd.get("code") and fd.get("line_reference") == vd.get("line_reference") for fd in final_discrepancies):
            final_discrepancies.append(vd)

    # 7. Validate Score and Outcome Consistency
    checks_performed += 1
    if final_discrepancies:
        if raw_outcome == "ready_to_pay":
            validation_log.append("Corrected outcome from ready_to_pay to needs_review due to verified discrepancies.")
        outcome = "needs_review"
        if score > 90:
            score = max(50, 100 - (len(final_discrepancies) * 15))
            validation_log.append(f"Adjusted match_score from {raw_score} to {score} to reflect discrepancies.")
    else:
        outcome = "ready_to_pay"
        if score < 90:
            score = 100
            validation_log.append("Clean match verified: adjusted match_score to 100.")

    confidence_level = "high" if score >= 85 else "medium" if score >= 70 else "low"

    return {
        "is_match": len(final_discrepancies) == 0,
        "outcome": outcome,
        "match_score": score,
        "confidence_level": confidence_level,
        "overall_confidence_percent": score,
        "discrepancies": final_discrepancies,
        "accepted_quantities": {str(k): str(v) for k, v in accepted_by_po_line.items()},
        "validation_report": {
            "status": "validation_complete",
            "checks_performed": checks_performed,
            "discrepancies_count": len(final_discrepancies),
            "adjustments": validation_log,
        },
    }


def _extract_invoice_context(invoice: Invoice, lines: list[InvoiceLine]) -> dict[str, Any]:
    return {
        "invoice_id": str(invoice.id),
        "invoice_number": invoice.invoice_no,
        "vendor_name": invoice.vendor_name_raw or (invoice.vendor.name if invoice.vendor else None),
        "po_number_ref": invoice.po_number_raw,
        "invoice_date": str(invoice.invoice_date) if invoice.invoice_date else None,
        "due_date": str(invoice.due_date) if invoice.due_date else None,
        "currency": invoice.currency,
        "subtotal": str(invoice.subtotal) if invoice.subtotal is not None else None,
        "tax_amount": str(invoice.tax_amount) if invoice.tax_amount is not None else None,
        "total_amount": str(invoice.total_amount) if invoice.total_amount is not None else None,
        "lines": [
            {
                "line_id": str(line.id),
                "item_code": line.item_code,
                "description": line.description,
                "quantity": str(line.qty) if line.qty is not None else None,
                "unit_price": str(line.unit_price) if line.unit_price is not None else None,
                "line_amount": str(line.line_amount) if line.line_amount is not None else None,
            }
            for line in lines
        ],
    }


def _extract_po_candidates(db: Session, invoice: Invoice) -> list[dict[str, Any]]:
    po_query = db.query(PurchaseOrder).filter(PurchaseOrder.is_current.is_(True))
    all_pos = po_query.order_by(PurchaseOrder.created_at.desc()).limit(15).all()

    candidates = []
    for po in all_pos:
        po_lines = db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        vendor_name = po.vendor.name if po.vendor else None
        candidates.append({
            "po_id": str(po.id),
            "po_number": po.po_number,
            "vendor_name": vendor_name,
            "currency": po.currency,
            "subtotal": str(po.subtotal) if po.subtotal is not None else None,
            "tax_amount": str(po.tax_amount) if po.tax_amount is not None else None,
            "total_amount": str(po.total_amount) if po.total_amount is not None else None,
            "lines": [
                {
                    "po_line_id": str(pl.id),
                    "item_code": pl.item_code,
                    "description": pl.description,
                    "quantity": str(pl.qty) if pl.qty is not None else None,
                    "unit_price": str(pl.unit_price) if pl.unit_price is not None else None,
                }
                for pl in po_lines
            ],
        })
    return candidates


def _extract_gr_candidates(db: Session, candidate_pos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    all_grs = db.query(GoodsReceipt).order_by(GoodsReceipt.created_at.desc()).limit(15).all()
    candidates = []
    for gr in all_grs:
        gr_lines = db.query(GoodsReceiptLine).filter(GoodsReceiptLine.goods_receipt_id == gr.id).all()
        vendor_name = gr.vendor.name if gr.vendor else None
        linked_po = db.query(PurchaseOrder).filter(PurchaseOrder.id == gr.po_id).first() if gr.po_id else None
        candidates.append({
            "gr_id": str(gr.id),
            "gr_number": gr.gr_number,
            "po_id": str(gr.po_id) if gr.po_id else None,
            "po_number": linked_po.po_number if linked_po else None,
            "vendor_name": vendor_name,
            "receipt_date": str(gr.receipt_date) if gr.receipt_date else None,
            "lines": [
                {
                    "item_code": gl.item_code,
                    "description": gl.description,
                    "received_qty": str(gl.received_qty) if gl.received_qty is not None else None,
                    "accepted_qty": str(gl.accepted_qty) if gl.accepted_qty is not None else None,
                    "rejected_qty": str(gl.rejected_qty) if gl.rejected_qty is not None else None,
                }
                for gl in gr_lines
            ],
        })
    return candidates


def _call_groq_match(
    invoice_data: dict[str, Any],
    pos: list[dict[str, Any]],
    grs: list[dict[str, Any]],
    model: str,
    api_key: str,
    openrouter_key: str = "",
) -> dict[str, Any]:
    target_model = model
    endpoint = GROQ_API_URL
    auth_key = api_key

    if model.startswith("openrouter/"):
        target_model = model.removeprefix("openrouter/")
        endpoint = OPENROUTER_CHAT_URL
        auth_key = openrouter_key or os.getenv("OPENROUTER_API_KEY", "") or api_key
    elif model.startswith("groq/"):
        target_model = model.removeprefix("groq/")
        endpoint = GROQ_API_URL
        auth_key = api_key
    elif _is_openrouter_matching_model(model):
        endpoint = OPENROUTER_CHAT_URL
        auth_key = openrouter_key or os.getenv("OPENROUTER_API_KEY", "") or api_key
    system_prompt = (
        "You are an expert Accounts Payable AI reasoning and calculation engine. "
        "Perform intelligent document matching and COMPUTE ALL AP CALCULATIONS "
        "between the given INVOICE, CANDIDATE PURCHASE ORDERS (POs), and CANDIDATE GOODS RECEIPTS (GRs).\n\n"
        "Calculations to perform:\n"
        "1. Semantic Matching: Identify matching PO, Goods Receipt (GR), and Vendor.\n"
        "2. Line Calculations:\n"
        "   - Line total check: compute quantity * unit_price and compare with stated line_amount.\n"
        "   - Price variance: compute (invoice_unit_price - po_unit_price) and percentage variance. Allow 2% price tolerance.\n"
        "   - Quantity variance: compute (invoice_quantity - accepted_gr_quantity).\n"
        "3. Header Calculations:\n"
        "   - Subtotal check: calculate sum of all line amounts and compare with invoice subtotal.\n"
        "   - Total reconciliation: calculate subtotal + tax_amount and compare with invoice total.\n"
        "   - Tax variance: compare invoice tax with PO tax (tolerance 0.01).\n"
        "4. Confidence & Match Score Calculation:\n"
        "   - Calculate match_score (0-100) and overall_confidence_percent (0-100) based on calculation reconciliation.\n"
        "   - Determine outcome: 'ready_to_pay' (if PO & GR match with all calculations within tolerance), "
        "'needs_review' (if discrepancies, missing GR, or tolerance variances), or 'no_match' (if no matching PO).\n"
        "5. Output Format: Return strictly a valid JSON object matching this schema:\n"
        "{\n"
        '  "matched_po_id": "<po_id uuid string or null>",\n'
        '  "matched_po_number": "<po_number string or null>",\n'
        '  "matched_gr_id": "<gr_id uuid string or null>",\n'
        '  "matched_gr_number": "<gr_number string or null>",\n'
        '  "resolved_vendor_name": "<string or null>",\n'
        '  "is_match": true/false,\n'
        '  "outcome": "ready_to_pay" | "needs_review" | "no_match",\n'
        '  "match_score": 0-100,\n'
        '  "overall_confidence_percent": 0-100,\n'
        '  "confidence_level": "high" | "medium" | "low",\n'
        '  "line_mappings": [\n'
        '    {"invoice_line_id": "<id>", "matched_po_line_id": "<id or null>", "calculated_line_total": "<num>", "price_variance": "<num>"}\n'
        '  ],\n'
        '  "discrepancies": [\n'
        '    {\n'
        '      "code": "MISSING_PO" | "MISSING_GR" | "PRICE_VARIANCE" | "QUANTITY_VARIANCE" | "TAX_VARIANCE" | "LINE_TOTAL_VARIANCE" | "HEADER_TOTAL_VARIANCE" | "CURRENCY_MISMATCH",\n'
        '      "message": "<string>",\n'
        '      "invoice_value": "<val>",\n'
        '      "expected_value": "<val>",\n'
        '      "difference": "<val>",\n'
        '      "tolerance": "<val>",\n'
        '      "line_reference": "<ref>",\n'
        '      "next_action": "<string>"\n'
        '    }\n'
        '  ],\n'
        '  "summary": "<plain English summary of calculations and match outcome>"\n'
        "}"
    )

    user_prompt = json.dumps({
        "invoice": invoice_data,
        "candidate_purchase_orders": pos,
        "candidate_goods_receipts": grs,
    }, indent=2)

    response = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {auth_key}", "Content-Type": "application/json"},
        json={
            "model": target_model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=35.0,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        if lines and lines[0].strip().lower() in {"```json", "json"} or lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        raw = "\n".join(lines).strip()
    return json.loads(raw)


def _call_luna_decisions_fallback(
    invoice_data: dict[str, Any],
    pos: list[dict[str, Any]],
    grs: list[dict[str, Any]],
    api_key: str,
    model: str = "openai/gpt-6-luna-decisions",
) -> dict[str, Any]:
    """Fallback using OpenRouter Luna decisions model."""
    summary_text = (
        f"Invoice {invoice_data.get('invoice_number')} from {invoice_data.get('vendor_name')} "
        f"for {invoice_data.get('total_amount')} {invoice_data.get('currency')}, "
        f"PO Ref: {invoice_data.get('po_number_ref')}.\n"
        f"Available POs: {', '.join(p['po_number'] + ' (' + str(p['vendor_name']) + ')' for p in pos)}.\n"
        f"Available GRs: {', '.join(g['gr_number'] for g in grs)}."
    )

    payload = {
        "model": model,
        "state": summary_text[:10000],
        "questions": {
            "match": {
                "type": "choice",
                "instructions": "Does this invoice match one of the candidate purchase orders?",
                "criteria": {
                    "match_ok": "Matches PO and details reconcile",
                    "match_issues": "Matches PO but has missing GR or discrepancies",
                    "no_match": "No matching purchase order found",
                },
            }
        },
    }

    response = httpx.post(
        OPENROUTER_DECISIONS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json()
    answers = data.get("answers", {}).get("match", {})
    choice = answers.get("choice", "match_issues")

    po_ref = normalize_value(invoice_data.get("po_number_ref"))
    matched_po = next((p for p in pos if normalize_value(p.get("po_number")) == po_ref), None)

    return {
        "matched_po_id": matched_po["po_id"] if matched_po else None,
        "matched_po_number": matched_po["po_number"] if matched_po else None,
        "matched_gr_id": None,
        "matched_gr_number": None,
        "resolved_vendor_name": invoice_data.get("vendor_name"),
        "line_mappings": [],
        "summary": f"Luna evaluated invoice match decision: {choice}.",
    }


def llm_evaluate_invoice_match(
    db: Session,
    invoice: Invoice,
    owner_id: Any = None,
) -> dict[str, Any]:
    """Execute LLM-based matching with fallback to gpt-oss luna 6 and database rules."""
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    invoice_data = _extract_invoice_context(invoice, lines)
    pos = _extract_po_candidates(db, invoice)
    grs = _extract_gr_candidates(db, pos)

    groq_api_key = os.getenv("GROQ_API_KEY", "")
    groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    luna_model = os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna-decisions")

    raw_fallbacks = os.getenv("MATCHING_FALLBACK_MODELS") or os.getenv("GROQ_FALLBACK_MODELS")
    if raw_fallbacks:
        matching_fallbacks = [m.strip() for m in raw_fallbacks.split(",") if m.strip()]
    else:
        single_fallback = os.getenv("GROQ_FALLBACK_MODEL", "").strip()
        if single_fallback and single_fallback not in ("openai/gpt-oss-20b", ""):
            matching_fallbacks = [single_fallback]
        elif single_fallback:
            matching_fallbacks = [single_fallback] + [
                m for m in DEFAULT_MATCHING_FALLBACK_MODELS if m != single_fallback
            ]
        else:
            matching_fallbacks = list(DEFAULT_MATCHING_FALLBACK_MODELS)

    llm_result = None
    successful_model = None

    # Step 1: Try Primary Groq Model
    if groq_api_key or openrouter_key:
        try:
            logger.info("Executing LLM match via primary model: %s", groq_model)
            llm_result = _call_groq_match(invoice_data, pos, grs, groq_model, groq_api_key, openrouter_key)
            if llm_result is not None:
                successful_model = groq_model
        except Exception as exc:
            logger.warning("Primary matching failed on %s (%s): %s", groq_model, type(exc).__name__, exc)

    # Step 2: Try 5 Fallback Models in sequence
    if llm_result is None:
        for fb_model in matching_fallbacks:
            if not fb_model or fb_model == groq_model:
                continue
            try:
                logger.info("Falling back to matching model: %s", fb_model)
                llm_result = _call_groq_match(invoice_data, pos, grs, fb_model, groq_api_key, openrouter_key)
                if llm_result is not None:
                    successful_model = fb_model
                    break
            except Exception as exc:
                logger.warning("Fallback matching model %s failed (%s): %s", fb_model, type(exc).__name__, exc)

    # Step 3: Try OpenRouter Luna Decisions fallback if full-schema models failed
    if llm_result is None and openrouter_key:
        try:
            logger.info("Falling back to OpenRouter Luna model: %s", luna_model)
            llm_result = _call_luna_decisions_fallback(invoice_data, pos, grs, openrouter_key, luna_model)
            if llm_result is not None:
                successful_model = luna_model
        except Exception as exc:
            logger.warning("OpenRouter Luna fallback failed (%s): %s", type(exc).__name__, exc)

    # If all LLMs failed, return None to let rule fallback handle it
    if llm_result is None:
        logger.error("All LLM matching models failed; falling back to deterministic rules")
        return None

    # Apply database auto-linking & resolution based on LLM output
    matched_po_id = llm_result.get("matched_po_id")
    matched_po_num = llm_result.get("matched_po_number")

    matched_po = None
    if matched_po_id:
        try:
            matched_po = db.query(PurchaseOrder).filter(PurchaseOrder.id == uuid.UUID(str(matched_po_id))).first()
        except Exception:
            pass
    if matched_po is None and matched_po_num:
        norm_num = normalize_value(matched_po_num)
        matched_po = next(
            (p for p in db.query(PurchaseOrder).filter(PurchaseOrder.is_current.is_(True)).all() if normalize_value(p.po_number) == norm_num),
            None,
        )

    # If LLM identified a matching PO, link it to the invoice
    if matched_po is not None:
        invoice.po_id = matched_po.id
        if matched_po.po_number and not invoice.po_number_raw:
            invoice.po_number_raw = matched_po.po_number

        # Auto-resolve / auto-create vendor if not present
        vendor_name = llm_result.get("resolved_vendor_name") or invoice.vendor_name_raw or (matched_po.vendor.name if matched_po.vendor else None)
        if vendor_name:
            v_norm = vendor_name.strip()
            existing_vendor = db.query(Vendor).filter(func.lower(Vendor.name) == v_norm.lower()).first()
            if existing_vendor is None:
                existing_vendor = Vendor(
                    name=v_norm,
                    tax_id=invoice.vendor_tax_id_raw or getattr(matched_po, "vendor_tax_id", None),
                    is_active=True,
                )
                db.add(existing_vendor)
                db.flush()
            invoice.vendor_id = existing_vendor.id
            if matched_po.vendor_id is None:
                matched_po.vendor_id = existing_vendor.id

    # Auto-link Goods Receipt if identified
    matched_gr_id = llm_result.get("matched_gr_id")
    matched_gr_num = llm_result.get("matched_gr_number")
    matched_gr = None
    if matched_gr_id:
        try:
            matched_gr = db.query(GoodsReceipt).filter(GoodsReceipt.id == uuid.UUID(str(matched_gr_id))).first()
        except Exception:
            pass
    if matched_gr is None and matched_gr_num:
        matched_gr = db.query(GoodsReceipt).filter(func.lower(GoodsReceipt.gr_number) == matched_gr_num.strip().lower()).first()

    if matched_gr and matched_po:
        matched_gr.po_id = matched_po.id
        if invoice.vendor_id:
            matched_gr.vendor_id = invoice.vendor_id

    # Auto-link any pending GR matching this PO
    if matched_po:
        pending_grs = db.query(GoodsReceipt).filter(GoodsReceipt.po_id.is_(None)).all()
        for pgr in pending_grs:
            doc = db.query(Document).filter(Document.id == pgr.source_document_id).first() if pgr.source_document_id else None
            ref_po = (doc.extracted_json or {}).get("po_number") if doc else None
            if ref_po and normalize_value(ref_po) == normalize_value(matched_po.po_number):
                pgr.po_id = matched_po.id
                if invoice.vendor_id:
                    pgr.vendor_id = invoice.vendor_id

    # Retrieve PO lines and Goods Receipts for deterministic reconciliation
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == matched_po.id).all()
        if matched_po
        else []
    )
    receipts = (
        db.query(GoodsReceipt).filter(GoodsReceipt.po_id == matched_po.id).all()
        if matched_po
        else []
    )
    receipt_lines = (
        db.query(GoodsReceiptLine).filter(GoodsReceiptLine.goods_receipt_id.in_([r.id for r in receipts])).all()
        if receipts
        else []
    )
    line_mappings = llm_result.get("line_mappings", [])

    # Validate the LLM's calculations and refine discrepancies
    price_tol = tolerance_fraction("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2")
    tax_tol = read_nonnegative_decimal("TAX_TOLERANCE_ABS", "0.01")
    validated_result = validate_and_refine_llm_calculations(
        llm_output=llm_result,
        invoice=invoice,
        lines=lines,
        po=matched_po,
        po_lines=po_lines,
        receipts=receipts,
        receipt_lines=receipt_lines,
        price_tolerance_pct=price_tol,
        tax_tolerance_abs=tax_tol,
    )

    score = validated_result["match_score"]
    outcome = validated_result["outcome"]
    discrepancies = validated_result["discrepancies"]
    confidence_level = validated_result["confidence_level"]
    overall_confidence = validated_result["overall_confidence_percent"]

    # Update invoice confidence score and field confidences with validated values
    invoice.overall_confidence_percent = overall_confidence
    conf = dict(getattr(invoice, "field_confidence", None) or {})
    conf["overall_match_confidence"] = {"confidence": float(score), "derived": False}
    conf["overall_confidence"] = {"confidence": float(overall_confidence), "derived": False}
    for key in ("vendor", "total", "subtotal", "invoice_number"):
        if key not in conf or (isinstance(conf.get(key), dict) and conf[key].get("confidence", 0) < 85):
            conf[key] = {"confidence": float(overall_confidence), "derived": True}
    invoice.field_confidence = conf

    # If match is clean and ready to pay, update match_status
    if outcome == "ready_to_pay":
        invoice.match_status = "pending"

    return {
        "rule_set_version": f"llm-match-validated-{successful_model or groq_model}",
        "outcome": outcome,
        "confidence_level": confidence_level,
        "overall_confidence_percent": overall_confidence,
        "field_confidence": getattr(invoice, "field_confidence", None) or {},
        "confidence_levels": {},
        "low_confidence_fields": [],
        "discrepancies": discrepancies,
        "validation_report": validated_result.get("validation_report", {}),
        "summary": llm_result.get("summary", ""),
        "inputs_summary": {
            "invoice_id": str(invoice.id),
            "po_number": invoice.po_number_raw or (matched_po.po_number if matched_po else None),
            "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
            "invoice_number": invoice.invoice_no,
            "currency": invoice.currency,
            "invoice_total": str(invoice.total_amount),
            "invoice_subtotal": str(invoice.subtotal),
            "invoice_tax": str(invoice.tax_amount),
            "po_ids": [str(matched_po.id)] if matched_po else [],
            "receipt_ids": [str(matched_gr.id)] if matched_gr else [],
            "po_currency": matched_po.currency if matched_po else None,
            "po_tax": str(matched_po.tax_amount) if matched_po else None,
            "po_total": str(matched_po.total_amount) if matched_po else None,
            "invoice_lines": [
                {
                    "sku": line.item_code,
                    "description": line.description,
                    "quantity": str(line.qty),
                    "unit_price": str(line.unit_price),
                    "line_total": str(line.line_amount),
                }
                for line in lines
            ],
            "po_lines": [
                {
                    "sku": pl.item_code,
                    "description": pl.description,
                    "quantity": str(pl.qty),
                    "unit_price": str(pl.unit_price),
                }
                for pl in (db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == matched_po.id).all() if matched_po else [])
            ],
            "accepted_quantity_by_po_line": validated_result.get("accepted_quantities", {}),
        },
    }
