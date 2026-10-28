"""
LLM-first semantic matching and contextual interpretation services.

Responsibilities of the LLM:
- Identify and resolve likely invoice-to-PO and invoice-to-Goods-Receipt relationships using supplied records.
- Match invoice line items to PO line items when descriptions differ or SKU information is incomplete.
- Interpret descriptions, synonyms, abbreviations, and contextual similarities.
- Identify ambiguous or potentially incorrect document relationships.
- Interpret extracted document information and identify missing or conflicting fields.
- Explain discrepancies and recommend the appropriate next action.
- Return structured candidate relationship proposals referencing actual supplied records.

The LLM is NEVER trusted to calculate financial values, differences, percentages,
subtotals, taxes, totals, or make payment approval decisions. All mathematical calculations
and business rule enforcement are executed by deterministic Python code.
"""

import json
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.document_workflow_services import _owner_scope
from src.services.po_matching_services import normalize_value

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
    """Check if model identifier maps to OpenRouter endpoint."""
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


def _extract_invoice_context(invoice: Invoice, lines: list[InvoiceLine]) -> dict[str, Any]:
    """Extract invoice context for semantic LLM matching without asking LLM for calculations."""
    v_obj = getattr(invoice, "vendor", None)
    return {
        "invoice_id": str(getattr(invoice, "id", "")),
        "invoice_number": getattr(invoice, "invoice_no", None),
        "vendor_name": getattr(invoice, "vendor_name_raw", None) or (v_obj.name if v_obj else None),
        "po_number_ref": getattr(invoice, "po_number_raw", None),
        "invoice_date": str(invoice.invoice_date) if getattr(invoice, "invoice_date", None) else None,
        "due_date": str(invoice.due_date) if getattr(invoice, "due_date", None) else None,
        "currency": getattr(invoice, "currency", None),
        "subtotal": str(invoice.subtotal) if getattr(invoice, "subtotal", None) is not None else None,
        "tax_amount": str(invoice.tax_amount) if getattr(invoice, "tax_amount", None) is not None else None,
        "total_amount": str(invoice.total_amount) if getattr(invoice, "total_amount", None) is not None else None,
        "lines": [
            {
                "line_id": str(getattr(line, "id", "")),
                "item_code": getattr(line, "item_code", None),
                "description": getattr(line, "description", None),
                "quantity": str(line.qty) if getattr(line, "qty", None) is not None else None,
                "unit_price": str(line.unit_price) if getattr(line, "unit_price", None) is not None else None,
                "line_amount": str(line.line_amount) if getattr(line, "line_amount", None) is not None else None,
            }
            for line in lines
        ],
    }


def _extract_po_candidates(db: Session, invoice: Invoice, owner_id: Any = None) -> list[dict[str, Any]]:
    """Extract candidate POs scoped strictly to permitted user data scope, prioritizing exact reference matches."""
    po_query = db.query(PurchaseOrder).filter(
        PurchaseOrder.is_current.is_(True),
        *_owner_scope(PurchaseOrder, owner_id),
    )
    
    prioritized_pos: list[PurchaseOrder] = []
    seen_ids = set()

    # 1. Prioritize explicit PO ID match
    inv_po_id = getattr(invoice, "po_id", None)
    if inv_po_id:
        try:
            target_po = po_query.filter(PurchaseOrder.id == inv_po_id).first()
            if target_po and target_po.id not in seen_ids:
                prioritized_pos.append(target_po)
                seen_ids.add(target_po.id)
        except Exception:
            pass

    # 2. Prioritize exact PO number reference match
    po_raw = normalize_value(getattr(invoice, "po_number_raw", None))
    if po_raw:
        for p in po_query.all():
            if p.id not in seen_ids and normalize_value(p.po_number) == po_raw:
                prioritized_pos.append(p)
                seen_ids.add(p.id)

    # 3. Prioritize POs matching the resolved vendor
    inv_v_id = getattr(invoice, "vendor_id", None)
    if inv_v_id:
        for vp in po_query.filter(PurchaseOrder.vendor_id == inv_v_id).order_by(PurchaseOrder.created_at.desc()).limit(5).all():
            if vp.id not in seen_ids:
                prioritized_pos.append(vp)
                seen_ids.add(vp.id)

    # 4. Fill remaining candidate slots with recent POs within scope
    remaining = max(0, 15 - len(prioritized_pos))
    if remaining > 0:
        for rp in po_query.order_by(PurchaseOrder.created_at.desc()).limit(20).all():
            if rp.id not in seen_ids and len(prioritized_pos) < 15:
                prioritized_pos.append(rp)
                seen_ids.add(rp.id)

    candidates = []
    for po in prioritized_pos:
        po_lines = db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        v_obj = getattr(po, "vendor", None)
        vendor_name = v_obj.name if v_obj else None
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


def _extract_gr_candidates(db: Session, candidate_pos: list[dict[str, Any]], owner_id: Any = None) -> list[dict[str, Any]]:
    """Extract candidate Goods Receipts scoped strictly to permitted user data scope."""
    gr_query = db.query(GoodsReceipt).filter(*_owner_scope(GoodsReceipt, owner_id))
    all_grs = gr_query.order_by(GoodsReceipt.created_at.desc()).limit(15).all()
    candidates = []
    for gr in all_grs:
        gr_lines = db.query(GoodsReceiptLine).filter(GoodsReceiptLine.goods_receipt_id == gr.id).all()
        v_obj = getattr(gr, "vendor", None)
        vendor_name = v_obj.name if v_obj else None
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
    """
    Call LLM strictly for semantic understanding, line pairings, contextual interpretation,
    and discrepancy explanations. Financial calculations are explicitly excluded.
    """
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
        "You are an expert Accounts Payable AI semantic reasoning assistant.\n"
        "Your responsibility is ONLY semantic matching, contextual interpretation, and candidate relationship proposals "
        "between the given INVOICE, CANDIDATE PURCHASE ORDERS (POs), and CANDIDATE GOODS RECEIPTS (GRs).\n\n"
        "STRICT ANTI-HALLUCINATION RULES:\n"
        "1. DO NOT perform mathematical calculations or financial arithmetic. "
        "All calculations, variances, tolerances, subtotals, and payment approvals are strictly computed by deterministic Python code.\n"
        "2. NEVER invent PO numbers, receipt IDs, vendors, line items, or evidence not present in the supplied records.\n"
        "3. Only proposal references present in the supplied records are valid. Validate IDs and numbers strictly.\n"
        "4. If there are NO discrepancies and records match, the 'discrepancies' list MUST be an empty array: []. "
        "Do NOT invent or hallucinate discrepancies to fill schema fields.\n"
        "5. If an invoice line specifies an SKU/item code, do NOT map it to a PO line that has a different, conflicting SKU/item code.\n\n"
        "Tasks to perform:\n"
        "1. Candidate Relationship Identification:\n"
        "   - Identify the most likely matching PO (by po_id and po_number) from candidate POs, or null if none or ambiguous.\n"
        "   - Identify the most likely matching Goods Receipt (by gr_id and gr_number) from candidate GRs, or null if none.\n"
        "   - Identify the resolved vendor name from candidate records or invoice text.\n"
        "2. Semantic Line Matching:\n"
        "   - Match each invoice line item to its corresponding PO line item using descriptions, synonyms, abbreviations, SKU context, or line index.\n"
        "   - For each invoice line, propose matched_po_line_id from the selected PO's lines (or null if unmatchable).\n"
        "3. Contextual Discrepancies & Explanations:\n"
        "   - Identify any semantic, missing field, or contextual discrepancies (e.g. description ambiguity, unmapped lines, missing documentation).\n"
        "   - Provide an actionable explanation and recommended next action.\n"
        "   - If no discrepancies exist, return [].\n"
        "4. Output Format: Return strictly a valid JSON object matching this schema:\n"
        "{\n"
        '  "matched_po_id": "<po_id uuid string or null>",\n'
        '  "matched_po_number": "<po_number string or null>",\n'
        '  "matched_gr_id": "<gr_id uuid string or null>",\n'
        '  "matched_gr_number": "<gr_number string or null>",\n'
        '  "resolved_vendor_name": "<string or null>",\n'
        '  "confidence_reasoning": "<string>",\n'
        '  "line_mappings": [\n'
        '    {"invoice_line_id": "<id or index>", "matched_po_line_id": "<po_line_id uuid or null>", "match_confidence": 0-100, "reasoning": "<string>"}\n'
        '  ],\n'
        '  "discrepancies": [],\n'
        '  "summary": "<plain English summary of semantic matching findings>"\n'
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
    """Fallback proposal using OpenRouter Luna decisions model."""
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
        "discrepancies": [],
        "summary": f"Luna evaluated invoice relationship proposal: {choice}.",
    }


def llm_propose_matches(
    db: Session,
    invoice: Invoice,
    owner_id: Any = None,
) -> dict[str, Any] | None:
    """
    Execute LLM semantic matching proposal chain (primary Groq -> fallback models -> Luna).
    Returns structured candidate relationship proposals without computing financial values.
    Returns None if LLM is unavailable or all models fail.
    """
    groq_api_key = os.getenv("GROQ_API_KEY", "")
    groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    luna_model = os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna-decisions")

    if not groq_api_key and not openrouter_key:
        return None

    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    invoice_data = _extract_invoice_context(invoice, lines)
    pos = _extract_po_candidates(db, invoice, owner_id=owner_id)
    grs = _extract_gr_candidates(db, pos, owner_id=owner_id)

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

    # Step 1: Try Primary Groq Model
    if groq_api_key or openrouter_key:
        try:
            logger.info("Executing LLM semantic matching via primary model: %s", groq_model)
            llm_result = _call_groq_match(invoice_data, pos, grs, groq_model, groq_api_key, openrouter_key)
        except Exception as exc:
            logger.warning("Primary matching failed on %s (%s): %s", groq_model, type(exc).__name__, exc)

    # Step 2: Try Fallback Models in sequence
    if llm_result is None:
        for fb_model in matching_fallbacks:
            if not fb_model or fb_model == groq_model:
                continue
            try:
                logger.info("Falling back to matching model: %s", fb_model)
                llm_result = _call_groq_match(invoice_data, pos, grs, fb_model, groq_api_key, openrouter_key)
                if llm_result is not None:
                    break
            except Exception as exc:
                logger.warning("Fallback matching model %s failed (%s): %s", fb_model, type(exc).__name__, exc)

    # Step 3: Try OpenRouter Luna Decisions fallback if full-schema models failed
    if llm_result is None and openrouter_key:
        try:
            logger.info("Falling back to OpenRouter Luna model: %s", luna_model)
            llm_result = _call_luna_decisions_fallback(invoice_data, pos, grs, openrouter_key, luna_model)
        except Exception as exc:
            logger.warning("OpenRouter Luna fallback failed (%s): %s", type(exc).__name__, exc)

    if llm_result is None:
        logger.warning("All LLM matching models failed; falling back to deterministic Python matching")
        return None

    return llm_result


def llm_evaluate_invoice_match(
    db: Session,
    invoice: Invoice,
    owner_id: Any = None,
) -> dict[str, Any] | None:
    """
    Public entrypoint for LLM matching evaluation.
    Delegates to the unified deterministic engine to ensure that Python owns all
    numerical calculations and payment-readiness enforcement.
    """
    from src.services.ap_decision_engine import evaluate_invoice
    return evaluate_invoice(db, invoice, owner_id=owner_id)


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
    """
    Validate all calculations deterministically against official procurement records,
    tolerances, and state transition rules. Backward compatibility wrapper.
    """
    from src.services.ap_decision_engine import execute_deterministic_matching
    line_mappings = llm_output.get("line_mappings") if isinstance(llm_output, dict) else []
    discrepancies = llm_output.get("discrepancies") if isinstance(llm_output, dict) else []
    summary = llm_output.get("summary", "") if isinstance(llm_output, dict) else ""

    return execute_deterministic_matching(
        db=None,  # Pure memory calculation if db is None
        invoice=invoice,
        lines=lines,
        po=po,
        po_lines=po_lines,
        receipts=receipts,
        receipt_lines=receipt_lines,
        line_mappings=line_mappings,
        llm_discrepancies=discrepancies,
        llm_summary=summary,
        price_tolerance_pct=price_tolerance_pct,
        tax_tolerance_abs=tax_tolerance_abs,
    )
