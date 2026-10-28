import base64
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# API endpoints
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_EXTRACTION_FALLBACK_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-small-24b-instruct-2501",
    "qwen/qwen-2.5-72b-instruct",
]


def _is_openrouter_model(model: str) -> bool:
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

DOCUMENT_TYPES = {"invoice", "po", "goods_receipt", "other"}
_E2E_PAYLOAD_PATTERN = re.compile(
    r"GW_AP_E2E_JSON_BEGIN(.*?)GW_AP_E2E_JSON_END", re.DOTALL
)


def validate_llm_provider_config() -> None:
    """Reject the local stub outside E2E and require it for E2E deployments."""
    app_env = os.getenv("APP_ENV", "").casefold()
    provider = os.getenv("LLM_PROVIDER", "").casefold()
    if provider == "stub" and app_env != "e2e":
        raise RuntimeError("LLM_PROVIDER=stub requires APP_ENV=e2e")
    if app_env == "e2e" and provider != "stub":
        raise RuntimeError("APP_ENV=e2e requires LLM_PROVIDER=stub")


def is_e2e_stub_enabled() -> bool:
    """Whether deterministic synthetic-document parsing is enabled."""
    validate_llm_provider_config()
    return (
        os.getenv("APP_ENV", "").casefold() == "e2e"
        and os.getenv("LLM_PROVIDER", "").casefold() == "stub"
    )


def _e2e_mock_enabled() -> bool:
    """Keep the test hook and enable the provider stub only in isolated E2E."""
    enabled = os.getenv("AP_E2E_MOCK_EXTRACTION", "")
    app_env = os.getenv("APP_ENV", "")
    if enabled:
        if enabled != "1" or app_env != "test":
            raise RuntimeError(
                "AP_E2E_MOCK_EXTRACTION requires APP_ENV=test and value 1"
            )
        return True
    return is_e2e_stub_enabled()


def _e2e_payload(raw_text: str) -> dict[str, Any]:
    """Decode the deterministic JSON marker embedded in synthetic test PDFs."""
    match = _E2E_PAYLOAD_PATTERN.search(raw_text)
    if match is None:
        raise ValueError("E2E mock payload is missing from the synthetic document")
    try:
        encoded = re.sub(r"\s+", "", match.group(1))
        payload = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("E2E mock payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("E2E mock payload must be a JSON object")
    return payload


def extract_e2e_test_document_text(content: bytes) -> str:
    """Read synthetic PDFs without loading or downloading Docling models."""
    if not _e2e_mock_enabled():
        raise RuntimeError("The local E2E document parser is test-only")

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(content)
    try:
        raw_text = "\n".join(page.get_textpage().get_text_range() for page in document)
    finally:
        document.close()
    if _E2E_PAYLOAD_PATTERN.search(raw_text) is None:
        raise ValueError("Synthetic E2E PDF does not contain a mock payload")
    return raw_text


def classify_document(
    raw_text: str, *, with_confidence: bool = False
) -> str | tuple[str, float]:
    """Classify document text using OpenRouter Luna Decisions."""
    if _e2e_mock_enabled():
        doc_type = str(_e2e_payload(raw_text).get("document_type", "")).lower()
        if doc_type == "gr":
            doc_type = "goods_receipt"
        if doc_type not in DOCUMENT_TYPES:
            raise ValueError(f"Invalid E2E document type: {doc_type!r}")
        return (doc_type, 1.0) if with_confidence else doc_type

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "")

    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    if not model:
        raise ValueError("OPENROUTER_MODEL is not configured")

    payload = {
        "model": model,
        "state": raw_text[:20000],
        "questions": {
            "doc_type": {
                "type": "choice",
                "instructions": (
                    "Classify the document as invoice, po, goods_receipt, or other. "
                    "Use invoice for a supplier invoice or bill, "
                    "po for a purchase order, goods_receipt for a delivery receipt, "
                    "goods received note, or proof of delivery, and other for "
                    "all "
                    "remaining document types."
                ),
                "criteria": {
                    "invoice": "Supplier invoice or bill",
                    "po": "Purchase order",
                    "goods_receipt": (
                        "Goods receipt, delivery receipt, or goods received note"
                    ),
                    "other": "Any other document",
                },
            }
        },
    }

    response = httpx.post(
        OPENROUTER_DECISIONS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60.0,
    )
    response.raise_for_status()

    data = response.json()
    answers = data.get("answers", {})
    answer = answers.get("doc_type", {}) if isinstance(answers, dict) else {}

    choice: object = answer.get("choice") if isinstance(answer, dict) else None

    if isinstance(choice, str) and choice.strip().lower() == "gr":
        choice = "goods_receipt"

    if choice not in DOCUMENT_TYPES:
        raise ValueError(f"Luna returned an invalid document type: {choice!r}")
    doc_type = str(choice)

    confidence = (
        answer.get("confidence", data.get("confidence", 0.0))
        if isinstance(answer, dict)
        else 0.0
    )
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    return (doc_type, confidence) if with_confidence else doc_type


def _groq_extract(
    raw_text: str,
    doc_type: str,
    api_key: str,
    model: str,
    openrouter_api_key: str = "",
    temperature: float = 0.0,
    system_prompt_override: str | None = None,
) -> dict[str, Any]:
    """Extract structured document fields using a Groq or OpenRouter model."""
    endpoint = GROQ_API_URL
    auth_key = api_key
    target_model = model

    if model.startswith("openrouter/"):
        target_model = model.removeprefix("openrouter/")
        endpoint = OPENROUTER_CHAT_URL
        auth_key = openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "") or api_key
    elif model.startswith("groq/"):
        target_model = model.removeprefix("groq/")
        endpoint = GROQ_API_URL
        auth_key = api_key
    elif _is_openrouter_model(model):
        endpoint = OPENROUTER_CHAT_URL
        auth_key = openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "") or api_key

    response = httpx.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {auth_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": target_model,
            "temperature": temperature,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Extract structured fields from this "
                        f"{doc_type} document. Return only a valid "
                        "JSON object. Do not invent values; use null "
                        "for missing values. Include document_type, "
                        "vendor, vendor_tax_id, invoice_number, order_number, "
                        "po_number, gr_number, receipt_date, received_qty, "
                        "accepted_qty, "
                        "rejected_qty, invoice_date, po_date, due_date, "
                        "currency, subtotal, tax, tax_rate, total, payment_terms, "
                        "delivery_details, delivery_address, ship_to, "
                        "expected_delivery_date, and line_items with item_code, "
                        "description, qty, unit_price, line_amount "
                        "when applicable. Include field_confidence as an object "
                        "with a 0-100 confidence and derived=false for vendor, "
                        "invoice_number, invoice_date, due_date, currency, "
                        "subtotal, tax, total, order_number, gl_code; include "
                        "the same confidence object for qty, unit_price, and "
                        "line_amount inside each line item. If confidence is "
                        "uncertain, use a lower score. "
                        "Preserve values as they appear in the source."
                    ),
                },
                {
                    "role": "user",
                    "content": raw_text[:30000],
                },
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()

    data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("LLM returned an unexpected response structure") from exc

    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM returned empty extraction content")

    content = content.strip()

    # Remove optional Markdown code fences.
    if content.startswith("```"):
        lines = content.splitlines()

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        if (
            lines
            and lines[0].strip().lower() in {"```json", "json"}
            or lines
            and lines[0].strip().startswith("```")
        ):
            lines = lines[1:]

        content = "\n".join(lines).strip()

    try:
        result: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM returned invalid JSON extraction content") from exc

    if not isinstance(result, dict):
        raise ValueError("Extractor did not return a JSON object")

    return result


def extract_document(
    raw_text: str,
    doc_type: str,
) -> dict[str, Any]:
    """Extract document fields with primary and 5 fallback models across Groq and OpenRouter."""
    if _e2e_mock_enabled():
        payload = _e2e_payload(raw_text)
        payload_type = str(payload.get("document_type", "")).lower()
        if payload_type == "gr":
            payload_type = "goods_receipt"
        if payload_type != doc_type:
            raise ValueError("E2E mock type does not match classified document")
        result = {
            key: value for key, value in payload.items() if key != "document_type"
        }
        return result

    groq_api_key = os.getenv("GROQ_API_KEY", "")
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    primary_model = os.getenv("GROQ_MODEL", "")

    if not groq_api_key and not openrouter_api_key:
        raise ValueError("GROQ_API_KEY is not configured")

    if not primary_model:
        raise ValueError("GROQ_MODEL is not configured")

    raw_fallbacks = os.getenv("EXTRACTION_FALLBACK_MODELS") or os.getenv("GROQ_FALLBACK_MODELS")
    if raw_fallbacks:
        fallback_models = [m.strip() for m in raw_fallbacks.split(",") if m.strip()]
    else:
        single_fallback = os.getenv("GROQ_FALLBACK_MODEL", "").strip()
        if single_fallback and single_fallback not in ("openai/gpt-oss-20b", ""):
            fallback_models = [single_fallback]
        elif single_fallback:
            fallback_models = [single_fallback] + [
                m for m in DEFAULT_EXTRACTION_FALLBACK_MODELS if m != single_fallback
            ]
        else:
            fallback_models = list(DEFAULT_EXTRACTION_FALLBACK_MODELS)

    # 1. Attempt primary model
    try:
        return _groq_extract(
            raw_text,
            doc_type,
            groq_api_key,
            primary_model,
            openrouter_api_key=openrouter_api_key,
        )
    except Exception as exc:
        logger.warning(
            "Primary extraction failed using model %s: %s",
            primary_model,
            exc,
            exc_info=True,
        )

        if not fallback_models:
            raise

    # 2. Iterate through fallback models
    last_exc = None
    for fallback_model in fallback_models:
        if not fallback_model or fallback_model == primary_model:
            continue
        logger.info(
            "Retrying extraction with fallback model %s",
            fallback_model,
        )
        try:
            return _groq_extract(
                raw_text,
                doc_type,
                groq_api_key,
                fallback_model,
                openrouter_api_key=openrouter_api_key,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Fallback extraction failed using model %s: %s",
                fallback_model,
                exc,
                exc_info=True,
            )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("All extraction models failed")


_CURRENCIES_PATTERN = r"INR|USD|EUR|GBP|RS\.?|RUPEES?"
_UOM_PATTERN = (
    r"PCS?|PIECES?|NOS?\.?|NUMBERS?|EA|EACH|UNITS?|"
    r"BOX(?:ES)?|PACK(?:S)?|PKTS?|SETS?|KGS?|KILOS?|GMS?|GRAMS?|"
    r"MTRS?|METERS?|LTRS?|LITRES?|DOZ(?:EN)?|HRS?|HOURS?|DAYS?|LOTS?|ROLLS?|BAGS?|PAIRS?|"
    r"CTN(?:S)?|CARTONS?|DRUMS?|CAN(?:S)?|BUNDLES?|"
    r"REAM(?:S)?|SHEETS?|PADS?|BOOKS?|BOTTLES?|PACKETS?|TINS?|BARRELS?|TONS?|TONNES?"
)


def _to_decimal(value: Any) -> Decimal | None:
    """Convert monetary or quantity values with currency codes, symbols, and UOM suffixes to Decimal."""
    if value is None or isinstance(value, bool):
        return None

    try:
        if isinstance(value, (int, float, Decimal)):
            result = Decimal(str(value))
            return result if result.is_finite() else None

        if not isinstance(value, str) or not value.strip():
            return None

        cleaned = value.strip()
        cleaned = re.sub(rf"(?i)\b(?:{_CURRENCIES_PATTERN})\b", "", cleaned)
        cleaned = re.sub(rf"(?i)(?<=\d)(?:{_UOM_PATTERN})\b", "", cleaned)
        cleaned = re.sub(rf"(?i)\b(?:{_UOM_PATTERN})\b", "", cleaned)
        cleaned = re.sub(r"[\$€£₹%,\s]", "", cleaned)

        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]

        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
            # Fallback: extract leading numeric portion if trailing text remains (e.g. "30 REAM" -> 30)
            m = re.match(r"^[+-]?\d+(?:\.\d+)?", cleaned)
            if m:
                cleaned = m.group(0)
            else:
                return None

        result = Decimal(cleaned)
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def validate_extracted_data(
    data: dict[str, Any],
    doc_type: str,
) -> list[str]:
    """Return review issues without modifying extracted values."""
    issues: list[str] = []

    if not isinstance(data, dict):
        return ["Extracted data must be a JSON object"]

    if doc_type not in DOCUMENT_TYPES:
        return [f"Unsupported document type: {doc_type}"]

    # Validate the document type returned by the extractor, if present.
    extracted_type = data.get("document_type")
    if extracted_type is not None:
        normalized_type = str(extracted_type).strip().lower()
        aliases = {
            "gst bill": "invoice",
            "purchase_order": "po",
            "purchase order": "po",
            "payment advice": "other",
            "payment_advice": "other",
            "gr": "goods_receipt",
            "goods receipt": "goods_receipt",
            "goods received note": "goods_receipt",
        }

        if aliases.get(normalized_type, normalized_type) != doc_type:
            issues.append(f"Document type mismatch: expected {doc_type}")

    if doc_type == "invoice":
        required_fields = (
            "vendor",
            "invoice_number",
            "invoice_date",
            "total",
        )
    elif doc_type == "po":
        required_fields = ("vendor", "total")
    elif doc_type == "goods_receipt":
        required_fields = ("vendor", "receipt_date")
    else:
        required_fields = ()

    for field in required_fields:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(f"Missing required field: {field}")

    if doc_type == "po" and not any(
        data.get(field) is not None
        and not (isinstance(data[field], str) and not data[field].strip())
        for field in ("order_number", "po_number")
    ):
        issues.append("Missing required field: order_number")

    if doc_type == "goods_receipt":
        if not any(
            data.get(field)
            for field in ("gr_number", "receipt_number", "goods_receipt_number")
        ):
            issues.append("Missing required field: gr_number")
        if not any(data.get(field) for field in ("order_number", "po_number")):
            issues.append("Missing required field: order_number")

    amounts = {
        field: _to_decimal(data.get(field)) for field in ("subtotal", "tax", "total")
    }

    for field, amount in amounts.items():
        value = data.get(field)
        if value is not None and amount is None:
            issues.append(f"Invalid monetary value: {field}")

    subtotal = amounts["subtotal"]
    tax = amounts["tax"]
    total = amounts["total"]

    # Compare amounts only when all three values are available.
    # Allow a one-cent rounding difference.
    if (
        subtotal is not None
        and tax is not None
        and total is not None
        and abs(subtotal + tax - total) > Decimal("0.01")
    ):
        issues.append("Amount mismatch: subtotal plus tax does not equal total")

    line_items = data.get("line_items")
    if line_items is not None:
        if not isinstance(line_items, list):
            issues.append("line_items must be a list")
        else:
            for index, item in enumerate(line_items):
                if not isinstance(item, dict):
                    issues.append(f"Line item {index + 1} must be an object")

    return issues


def test_validate_extracted_data_accepts_valid_invoice():
    data = {
        "document_type": "invoice",
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "subtotal": 1500,
        "tax": 0,
        "total": 1500,
        "line_items": [],
    }

    assert validate_extracted_data(data, "invoice") == []


def test_validate_extracted_data_detects_missing_invoice_number():
    data = {
        "vendor": "ABC Supplies",
        "invoice_date": "2026-10-01",
        "total": 1500,
    }

    issues = validate_extracted_data(data, "invoice")

    assert "Missing required field: invoice_number" in issues


def test_validate_extracted_data_detects_amount_mismatch():
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "subtotal": 1000,
        "tax": 100,
        "total": 1200,
    }

    issues = validate_extracted_data(data, "invoice")

    assert "Amount mismatch: subtotal plus tax does not equal total" in issues


def test_validate_extracted_data_detects_invalid_total():
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "total": "unknown",
    }

    issues = validate_extracted_data(data, "invoice")

    assert "Invalid monetary value: total" in issues


def test_validate_extracted_data_accepts_valid_purchase_order():
    data = {
        "document_type": "po",
        "vendor": "ABC Supplies",
        "order_number": "PO-101",
        "total": 2500,
    }

    assert validate_extracted_data(data, "po") == []


def test_validate_extracted_data_rejects_invalid_line_items():
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "total": 1500,
        "line_items": "not-a-list",
    }

    issues = validate_extracted_data(data, "invoice")

    assert "line_items must be a list" in issues
