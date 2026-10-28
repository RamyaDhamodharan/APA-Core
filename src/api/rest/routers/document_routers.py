import importlib
import inspect
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from minio.error import S3Error
from sqlalchemy import Float, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....api.rest.dependencies import get_current_user, require_role
from ....db.session_db import SessionLocal, get_db
from ....models.audit_models import AuditLog
from ....models.document_models import Document
from ....models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from ....models.invoice_match_models import InvoiceDiscrepancy
from ....models.invoice_models import Invoice, InvoiceLine
from ....models.match_run_models import MatchRun
from ....models.notification_models import Notification
from ....models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from ....models.vendor_models import Vendor
from ....schemas.document_schemas import (
    DocumentAuditEntry,
    DocumentListResponse,
    DocumentReviewDetail,
    DocumentSummary,
)
from ....schemas.purchase_order_schemas import (
    PurchaseOrderResponse,
    PurchaseOrderVendorResolutionRequest,
)
from ....services.docling_model_services import (
    OCR_MODELS_UNAVAILABLE_MESSAGE,
    docling_models_available,
)
from ....services.document_ingest_services import MAX_FILE_SIZE, ingest_file
from ....services.document_preview_services import (
    DocumentPreviewError,
    get_docx_preview,
)
from ....services.document_retry_services import (
    classify_document_failure,
    is_transient_document_error,
    with_transient_retries,
)
from ....services.document_workflow_services import refresh_related_workflows
from ....services.email_sender_services import may_send_automatic_reply
from ....services.invoice_confidence_services import (
    confidence_unavailable,
    normalize_invoice_confidence,
    requires_verification,
)
from ....services.invoice_matching_services import run_match_for_invoice
from ....services.invoice_number_services import (
    find_duplicate_invoice,
    normalize_invoice_no,
)
from ....services.invoice_state_services import (
    legacy_workflow_status,
    transition_invoice,
)
from ....services.llm_pipeline_services import (
    _to_decimal,
    classify_document,
    extract_document,
    extract_e2e_test_document_text,
    is_e2e_stub_enabled,
    validate_extracted_data,
)
from ....services.vendor_resolution_services import (
    find_active_alias_conflict,
    find_canonical_name_conflict,
    normalize_vendor_name,
    resolve_vendor,
)
from ....storage.minio_storage import (
    download_file,
    move_file,
)
from ....worker.dispatch import (
    dispatch_document_processing,
    dispatch_queued_notifications,
    dispatch_raw_email_recovery,
)

logger = logging.getLogger(__name__)


def _safe_exception_detail(error: Exception) -> str:
    """Return technical context with credential-like values removed for logs."""
    detail = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|"
        r"(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key)"
        r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[REDACTED]",
        str(error),
    )
    detail = re.sub(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)", r"\1[REDACTED]\2", detail)
    for name in (
        "IMAP_PASSWORD",
        "MINIO_SECRET_KEY",
        "JWT_SECRET_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
    ):
        secret = os.getenv(name, "")
        if secret and len(secret) >= 4:
            detail = detail.replace(secret, "[REDACTED]")
    return detail[:500]


def _record_pipeline_step(
    db,
    document: Document,
    step_name: str,
    started_at: float,
    *,
    input_ref: str | None = None,
    output_summary: dict | None = None,
    result: str = "success",
) -> None:
    """Write a compact, non-sensitive audit record for one pipeline step."""
    try:
        with db.begin_nested():
            db.add(
                AuditLog(
                    action="pipeline_step",
                    actor=(
                        str(getattr(document, "created_by", None))
                        if getattr(document, "created_by", None)
                        else "system"
                    ),
                    entity_type="document",
                    entity_id=document.id,
                    step_name=step_name,
                    duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
                    input_ref=(
                        input_ref
                        or getattr(document, "storage_path", None)
                        or str(document.id)
                    )[:500],
                    output_summary=output_summary or {},
                    result=result,
                    message=f"Document {step_name} step {result}",
                    created_by=getattr(document, "created_by", None),
                )
            )
            db.flush()
    except Exception as exc:
        logger.warning("Pipeline step audit write failed (%s)", type(exc).__name__)


def _auto_reject_unmatched_invoice(
    db, invoice: Invoice, document: Document | None
) -> None:
    """Reject a completed no-match and best-effort queue allowlisted email."""
    if os.getenv("EMAIL_NO_MATCH_AUTO_REJECT", "true").casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    reason = "No purchase order / unknown vendor"
    current = legacy_workflow_status(invoice)
    if current in {"approved", "paid", "rejected"}:
        return
    if current not in {"no_match", "rejected"}:
        transition_invoice(db, invoice, "no_match", changed_by=None, reason=reason)
    transition_invoice(db, invoice, "rejected", changed_by=None, reason=reason)
    db.add(
        AuditLog(
            action="invoice_no_match_rejected",
            actor="system",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"workflow_status": current},
            new_value={"workflow_status": "rejected", "reason": reason},
            result="warning",
            message=reason,
            reason=reason,
            created_by=None,
        )
    )
    if document is None or not getattr(document, "email_sender", None):
        return
    try:
        from src.services.email_draft_services import create_no_match_draft

        with db.begin_nested():
            draft = create_no_match_draft(db, invoice, document.email_sender)
            db.add(
                AuditLog(
                    action="draft_created",
                    actor="system",
                    entity_type="email_draft",
                    entity_id=draft.id,
                    result="success",
                    message="No-match reply draft created for associate review",
                    reason=reason,
                    created_by=None,
                )
            )
    except Exception:
        logger.warning("No-match email draft could not be created", exc_info=True)


router = APIRouter(prefix="/api/documents", tags=["Documents"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml", ".docx"}
STALE_PARSE_TIMEOUT = timedelta(minutes=10)

_PROCESS_SLOTS = threading.BoundedSemaphore(int(os.getenv("PROCESS_CONCURRENCY", "2")))

_converters: dict = {}


@router.post("/{document_id}/reclassify", status_code=202)
def reclassify_other_document(
    document_id: uuid.UUID,
    doc_type: Annotated[str, Body(embed=True)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Move an Other document back into the shared classification pipeline."""
    if doc_type not in {"invoice", "po", "goods_receipt"}:
        raise HTTPException(status_code=422, detail="Unsupported document type")
    user_id = _document_owner_id(current_user)
    document = (
        db.query(Document).filter(Document.id == document_id).with_for_update().first()
    )
    if document is None or document.status != "other":
        raise HTTPException(status_code=404, detail="Other document not found")
    old_key = document.storage_path
    filename = Path(document.original_filename).name
    now = (
        getattr(document, "email_received_at", None)
        or getattr(document, "created_at", None)
        or datetime.now(UTC)
    )
    new_key = f"{doc_type}/{now:%Y}/{now:%m}/{document.id}/{filename}"
    if old_key and old_key != new_key:
        move_file(old_key, new_key)
    document.storage_path = new_key
    document.status = "received"
    document.classify_override = doc_type
    document.source_flag = None
    document.failure_message = None
    document.updated_by = user_id
    db.add(
        AuditLog(
            action="document_reclassified",
            entity_type="document",
            entity_id=document.id,
            result="success",
            message="Other document reclassified",
            old_value={"doc_type": document.doc_type, "status": "other"},
            new_value={"doc_type": doc_type, "status": "received"},
            created_by=user_id,
        )
    )
    db.commit()
    dispatch_document_processing(
        background_tasks, document.id, _process_document_with_retries
    )
    return {"id": str(document.id), "status": "received", "doc_type": doc_type}


def _document_owner_id(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _document_owner_filters(owner_id: uuid.UUID) -> list:
    owned_invoices = select(Invoice.source_document_id).where(
        or_(Invoice.owner_user_id == owner_id, Invoice.created_by == owner_id)
    )
    return [
        or_(
            Document.created_by == owner_id,
            Document.id.in_(owned_invoices),
        )
    ]


def _document_scope_filters(owner_id: uuid.UUID, current_user: dict) -> list:
    """Mailbox documents are shared with finance staff; uploads remain user scoped."""
    if current_user.get("role") == "admin":
        return []
    return [or_(Document.source == "email", *_document_owner_filters(owner_id))]


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: Annotated[str | None, Query(max_length=40)] = None,
    doc_type: Annotated[str | None, Query(max_length=40)] = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
    parent_document_id: uuid.UUID | None = None,
    include_parents: bool = False,
    vendor: Annotated[str | None, Query(max_length=200)] = None,
    workflow_status: Annotated[str | None, Query(max_length=40)] = None,
    bucket: Annotated[str | None, Query(max_length=40)] = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    gl_code: Annotated[str | None, Query(max_length=60)] = None,
    owner_user_id: uuid.UUID | None = None,
    confidence_min: Annotated[float | None, Query(ge=0, le=100)] = None,
    sort_by: Annotated[
        str,
        Query(pattern="^(due_date|amount|days_overdue|confidence|date_received)$"),
    ] = "date_received",
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> DocumentListResponse:
    """List documents uploaded by the authenticated user."""
    owner_id = _document_owner_id(current_user)
    filters = _document_scope_filters(owner_id, current_user)
    if status:
        filters.append(Document.status == status)
    if doc_type:
        filters.append(Document.doc_type == doc_type)
    if source:
        if source == "email":
            filters.append(Document.source.in_(["email", "email_poll"]))
        else:
            filters.append(Document.source == source)
    if parent_document_id:
        filters.append(Document.parent_document_id == parent_document_id)
    if not include_parents:
        filters.append(
            or_(
                Document.email_message_id.is_(None),
                Document.parent_document_id.is_not(None),
            )
        )
    if vendor:
        filters.append(
            Document.extracted_json["vendor"].astext.ilike(f"%{vendor.strip()}%")
        )
    if workflow_status:
        filters.append(Invoice.workflow_status == workflow_status)
    if bucket:
        filters.append(Document.doc_type == bucket)
    if due_date_from:
        filters.append(Invoice.due_date >= due_date_from)
    if due_date_to:
        filters.append(Invoice.due_date <= due_date_to)
    if due_date_from and due_date_to and due_date_from > due_date_to:
        raise HTTPException(
            status_code=422, detail="due_date_from must not exceed due_date_to"
        )
    if amount_min is not None:
        filters.append(Invoice.total_amount >= amount_min)
    if amount_max is not None:
        filters.append(Invoice.total_amount <= amount_max)
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(
            status_code=422, detail="amount_min must not exceed amount_max"
        )
    if gl_code:
        filters.append(Invoice.gl_code == gl_code)
    if owner_user_id:
        filters.append(Document.created_by == owner_user_id)
    if confidence_min is not None:
        filters.append(
            cast(Invoice.field_confidence["total"]["confidence"].as_float(), Float)
            >= confidence_min
        )

    invoice_join = bool(
        workflow_status
        or due_date_from
        or due_date_to
        or amount_min is not None
        or amount_max is not None
        or gl_code
        or confidence_min is not None
        or sort_by != "date_received"
    )
    count_query = db.query(func.count(Document.id))
    page_query = db.query(Document)
    if invoice_join:
        count_query = count_query.outerjoin(
            Invoice, Invoice.source_document_id == Document.id
        )
        page_query = page_query.outerjoin(
            Invoice, Invoice.source_document_id == Document.id
        )
    total = count_query.filter(*filters).scalar() or 0
    rows = (
        page_query.filter(*filters)
        .order_by(
            {
                "due_date": Invoice.due_date.asc().nullslast(),
                "amount": Invoice.total_amount.desc().nullslast(),
                "days_overdue": Invoice.due_date.asc().nullslast(),
                "confidence": cast(
                    Invoice.field_confidence["total"]["confidence"].as_float(),
                    Float,
                )
                .asc()
                .nullslast(),
                "date_received": Document.created_at.desc(),
            }[sort_by],
            Document.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    linked_invoices = (
        {
            invoice.source_document_id: invoice
            for invoice in db.query(Invoice)
            .filter(Invoice.source_document_id.in_([row.id for row in rows]))
            .all()
        }
        if rows
        else {}
    )
    return DocumentListResponse(
        items=[
            DocumentSummary.model_validate(row).model_copy(
                update={
                    "source": (
                        "email" if row.source in {"email", "email_poll"} else row.source
                    ),
                    "linked_invoice_id": (
                        linked_invoices[row.id].id
                        if row.id in linked_invoices
                        else None
                    ),
                    "linked_invoice_status": (
                        linked_invoices[row.id].status
                        if row.id in linked_invoices
                        else None
                    ),
                }
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}", response_model=DocumentReviewDetail)
def get_document_detail(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> DocumentReviewDetail:
    """Return source/extraction evidence and audit history for its uploader."""
    owner_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_scope_filters(owner_id, current_user),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    events = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "document",
            AuditLog.entity_id == document_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    linked_invoice = (
        db.query(Invoice).filter(Invoice.source_document_id == document.id).first()
    )
    linked_po = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.source_document_id == document.id,
            or_(
                PurchaseOrder.created_by == owner_id,
                PurchaseOrder.source_document_id.in_(
                    db.query(Document.id).filter(Document.created_by == owner_id)
                ),
            ),
        )
        .first()
    )
    linked_gr = (
        db.query(GoodsReceipt)
        .filter(
            GoodsReceipt.source_document_id == document.id,
            or_(
                GoodsReceipt.created_by == owner_id,
                GoodsReceipt.source_document_id.in_(
                    db.query(Document.id).filter(Document.created_by == owner_id)
                ),
            ),
        )
        .first()
    )
    return DocumentReviewDetail.model_validate(
        {
            **DocumentSummary.model_validate(document).model_dump(),
            "source": (
                "email"
                if document.source in {"email", "email_poll"}
                else document.source
            ),
            "linked_invoice_id": linked_invoice.id if linked_invoice else None,
            "linked_invoice_status": linked_invoice.status if linked_invoice else None,
            "linked_po_id": linked_po.id if linked_po else None,
            "linked_gr_id": linked_gr.id if linked_gr else None,
            "raw_text": document.raw_text,
            "extracted_json": document.extracted_json,
            "audit_history": [
                DocumentAuditEntry.model_validate(event).model_dump()
                for event in events
            ],
        }
    )


def _get_converter(converter_cls):
    if converter_cls not in _converters:
        _converters[converter_cls] = converter_cls()
    return _converters[converter_cls]


def _docling_models_missing() -> bool:
    return not docling_models_available()


def _is_extraction_model_error(error: Exception) -> bool:
    """Identify missing local model artifacts without masking parser failures."""
    current: BaseException | None = error
    while current is not None:
        error_text = str(current).casefold()
        if type(current).__name__ == "LocalEntryNotFoundError" or any(
            marker in error_text
            for marker in (
                "localentrynotfound",
                "huggingface",
                "model artifact",
                "model repository",
                "could not load model",
                "snapshot_download",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _extract_pdf_text_layer(path: str) -> str:
    """Read embedded PDF text when Docling's layout/OCR model is unavailable."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(path)
    pages = []
    for page in pdf:
        text_page = page.get_textpage()
        pages.append(text_page.get_text_range())
        text_page.close()
        page.close()
    pdf.close()
    return "\n\n".join(page_text for page_text in pages if page_text.strip())


def _route_document_to_other(
    db: Session, document: Document, doc_type: str, confidence: float
) -> None:
    """Move a low-confidence classification out before extraction or matching."""
    old_key = document.storage_path
    now = (
        getattr(document, "email_received_at", None)
        or getattr(document, "created_at", None)
        or datetime.now(UTC)
    )
    filename = Path(document.original_filename).name
    new_key = f"other/{now:%Y}/{now:%m}/{document.id}/{filename}"
    if old_key and old_key != new_key:
        move_file(old_key, new_key)
    document.storage_path = new_key
    document.status = "other"
    document.failure_message = "Not an invoice."
    db.add(
        AuditLog(
            action="document_routed_other",
            entity_type="document",
            entity_id=document.id,
            result="success",
            message="Document routed to other",
            new_value={"doc_type": doc_type, "confidence": confidence},
            created_by=getattr(document, "created_by", None),
        )
    )
    db.commit()


def _classify_document_cheaply(
    filename: str, raw_text: str
) -> tuple[str, float] | None:
    """Use clear filename/first-page labels before the existing paid classifier."""
    first_page = raw_text.split("\f", 1)[0][:6000].casefold()
    filename = Path(filename).stem.casefold().replace("_", " ").replace("-", " ")
    signals = (
        ("invoice", r"\binvoice\b|\btax invoice\b|\bbill to\b", "invoice"),
        ("po", r"\bpurchase order\b|\bpo number\b|\bpo no\b", "po"),
        (
            "goods_receipt",
            r"\bgoods received note\b|\bdelivery receipt\b|"
            r"\bproof of delivery\b|\bGRN\b",
            "goods_receipt",
        ),
    )
    for doc_type, pattern, filename_pattern in signals:
        if re.search(filename_pattern.replace("\\b", " "), filename) or re.search(
            pattern, first_page, re.IGNORECASE
        ):
            return doc_type, 0.95
    return None


def _normalize_invoice_value(value: object) -> str:
    """Normalize vendor names and invoice numbers for comparison."""
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _extracted_field_confidence(data: dict) -> dict:
    value = data.get("field_confidence") or data.get("confidence") or {}
    return value if isinstance(value, dict) else {}


def _parse_document_date(value: object) -> date | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    for fmt in (
        "%B %d, %Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue

    return None


def _purchase_order_number(data: dict) -> str:
    return str(data.get("po_number") or data.get("order_number") or "").strip()


def _find_duplicate_purchase_order(
    db: Session,
    po_number: str,
    *,
    exclude_document_id: uuid.UUID | None = None,
) -> PurchaseOrder | None:
    normalized_number = _normalize_invoice_value(po_number)
    if not normalized_number:
        return None

    query = db.query(PurchaseOrder).filter(
        func.lower(
            func.regexp_replace(func.trim(PurchaseOrder.po_number), r"\s+", " ", "g")
        )
        == normalized_number,
        PurchaseOrder.is_current.is_(True),
    )
    if exclude_document_id is not None:
        query = query.filter(PurchaseOrder.source_document_id != exclude_document_id)
    return query.first()


def _purchase_order_persistence_issues(data: dict) -> list[str]:
    issues = []
    po_number = _purchase_order_number(data)
    po_date = data.get("po_date") or data.get("order_date")
    currency = str(data.get("currency") or "").strip()
    total = _to_decimal(data.get("total"))

    if not po_number:
        issues.append("Missing required field: order_number")
    elif len(po_number) > 60:
        issues.append("Invalid purchase order number: maximum length is 60")

    if not po_date:
        issues.append("Missing required field: po_date")
    elif _parse_document_date(po_date) is None:
        issues.append("Invalid date value: po_date")

    if not currency:
        issues.append("Missing required field: currency")
    elif not re.fullmatch(r"[A-Za-z]{3}", currency):
        issues.append("Invalid currency: expected a three-letter code")

    if data.get("total") is not None and total is None:
        issues.append("Invalid monetary value: total")

    line_items = data.get("line_items")
    if line_items is not None and isinstance(line_items, list):
        line_amounts = []
        all_line_amounts_present = bool(line_items)
        for index, line in enumerate(line_items, start=1):
            if not isinstance(line, dict):
                continue  # validate_extracted_data reports the object error.
            quantity = _to_decimal(_line_value(line, "qty", "quantity"))
            if quantity is None or quantity <= 0:
                issues.append(f"PO line item {index} has a missing or invalid quantity")
            for field, aliases in (
                ("unit price", ("unit_price", "price")),
                ("line amount", ("line_amount", "amount", "total")),
            ):
                raw_amount = _line_value(line, *aliases)
                if raw_amount is not None and _to_decimal(raw_amount) is None:
                    issues.append(f"PO line item {index} has an invalid {field}")
            raw_line_amount = _line_value(line, "line_amount", "amount", "total")
            parsed_line_amount = _to_decimal(raw_line_amount)
            if parsed_line_amount is None:
                all_line_amounts_present = False
            else:
                line_amounts.append(parsed_line_amount)

        subtotal = _to_decimal(data.get("subtotal"))
        if (
            subtotal is not None
            and all_line_amounts_present
            and abs(sum(line_amounts, Decimal(0)) - subtotal) > Decimal("0.01")
        ):
            issues.append("PO line amounts do not reconcile to subtotal")

    return issues


def _process_in_background(document_id: uuid.UUID) -> None:
    """Automatically run document parsing and extraction."""
    with _PROCESS_SLOTS:
        db = SessionLocal()

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document is None or document.status != "received":
                return
            if document.created_by is None and document.source not in {
                "email",
                "email_poll",
            }:
                logger.error(
                    "Skipping processing for document %s without an uploader",
                    document_id,
                )
                document.status = "failed"
                document.failure_class = "permanent"
                document.failure_message = (
                    "Document processing could not start because no uploader was "
                    "recorded. Ask an administrator to retry processing."
                )
                db.commit()
                return

            worker_user = {"sub": str(document.created_by)}
            if document.created_by is None:
                worker_user = {"sub": "system-worker", "worker": True}
            parse_document(
                document_id=document_id,
                background_tasks=None,
                db=db,
                current_user=worker_user,
            )

        except HTTPException as exc:
            logger.error(
                "Auto processing failed for %s: %s",
                document_id,
                exc.detail,
            )
            _mark_unhandled_processing_failure(db, document_id, exc)

        except Exception as exc:
            logger.error(
                "Auto processing crashed for %s (%s): %s",
                document_id,
                type(exc).__name__,
                _safe_exception_detail(exc),
            )
            _mark_unhandled_processing_failure(db, document_id, exc)

        finally:
            db.close()


def _mark_unhandled_processing_failure(
    db: Session, document_id: uuid.UUID, error: BaseException
) -> None:
    """Persist an escaped worker error so the bounded retry policy can act."""
    try:
        db.rollback()
        document = (
            db.query(Document)
            .filter(Document.id == document_id)
            .with_for_update()
            .first()
        )
        if document is None or document.status in {
            "failed",
            "needs_data_review",
            "needs_vendor_review",
            "extracted",
            "processed",
            "ignored",
        }:
            return
        status_code = getattr(error, "status_code", None)
        if status_code is not None and status_code >= 500:
            failure_class = "retryable"
        elif status_code is not None and status_code >= 400:
            failure_class = "permanent"
        else:
            failure_class = classify_document_failure(error)
        document.status = "failed"
        document.failure_class = failure_class
        document.next_retry_at = None
        document.failure_message = (
            "A temporary processing problem occurred. An automatic retry will be "
            "scheduled."
            if failure_class == "retryable"
            else "The document could not be processed. Check the file and try again."
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Could not persist escaped processing failure for %s", document_id
        )


def _process_document_with_retries(document_id: uuid.UUID) -> None:
    """Run the shared bounded retry policy in the non-Celery worker path."""
    from ....services.document_retry_services import process_document_with_retries

    process_document_with_retries(document_id, session_factory=SessionLocal)


@router.post("/upload", status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., alias="file"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    max_batch_size = int(os.getenv("MAX_BATCH_UPLOAD_FILES", "100"))
    if len(files) > max_batch_size:
        raise HTTPException(
            status_code=422,
            detail=f"You can upload up to {max_batch_size} files at a time",
        )
    results = []
    user_id = uuid.UUID(str(current_user["sub"]))
    max_upload_size = int(os.getenv("EMAIL_ATTACHMENT_MAX_BYTES", str(MAX_FILE_SIZE)))
    for file in files:
        filename = Path(file.filename or "upload").name
        content = await file.read(max_upload_size + 1) # to detect oversize files
        result = ingest_file(
            db,
            filename,
            content,
            file.content_type,
            user_id,
            "upload",
            background_tasks=background_tasks,
        )
        results.append(
            {
                "filename": filename,
                "document_id": str(result.document_id) if result.document_id else None,
                "status": result.status,
                "result": {
                    "received": "accepted",
                    "duplicate_blocked": "duplicate",
                    "ignored": "ignored non-bill",
                    "other": "ignored non-bill",
                    "failed": "failed",
                }.get(result.status, result.status),
                "message": result.message,
            }
        )
    response = {"results": results}
    if len(results) == 1:
        response.update(
            {
                "id": results[0]["document_id"],
                "original_filename": results[0]["filename"],
                "status": results[0]["status"],
                "message": results[0]["message"],
            }
        )
    return response


@router.post(
    "/admin/backfill-procurement", dependencies=[Depends(require_role("admin"))]
)
def backfill_procurement_documents_endpoint(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Persist existing extracted PO/GR documents that need data review."""
    from ....services.procurement_backfill_services import (
        backfill_procurement_documents,
    
    )

    actor_id = uuid.UUID(str(current_user["sub"])) if current_user.get("sub") else None
    return backfill_procurement_documents(db, actor_id)


@router.post("/admin/reprocess-all", dependencies=[Depends(require_role("admin"))])
def reprocess_documents_admin(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Explicitly reprocess one idempotent batch of at most five documents."""
    from ....services.document_retry_services import reprocess_documents_batch

    actor_id = uuid.UUID(str(current_user["sub"]))
    return reprocess_documents_batch(db, actor_id, limit=5)


def _save_invoice_from_extraction(
    db: Session,
    document: Document,
    data: dict,
) -> Invoice:
    """Create or refresh an invoice and evaluate its PO match."""

    source_invoice = (
        db.query(Invoice)
        .filter(Invoice.source_document_id == document.id)
        .with_for_update()
        .first()
    )

    vendor_name = str(data.get("vendor") or "").strip() or None
    vendor_resolution_started = time.perf_counter()
    vendor = resolve_vendor(db, vendor_name, tax_id=data.get("vendor_tax_id"))
    _record_pipeline_step(
        db,
        document,
        "vendor_resolve",
        vendor_resolution_started,
        output_summary={"resolved": vendor is not None},
        result="success" if vendor is not None else "warning",
    )

    invoice_no = str(data.get("invoice_number") or "").strip() or None

    po_number = (
        str(data.get("order_number") or data.get("po_number") or "").strip() or None
    )

    currency = str(data.get("currency") or "").strip().upper() or None

    subtotal = _to_decimal(data.get("subtotal"))
    tax_amount = _to_decimal(data.get("tax"))
    total_amount = _to_decimal(data.get("total"))

    po = None
    match_issues = []

    if po_number:
        po = (
            db.query(PurchaseOrder)
            .filter(
                func.lower(func.trim(PurchaseOrder.po_number))
                == _normalize_invoice_value(po_number),
                PurchaseOrder.is_current.is_(True),
            )
            .first()
        )

    if not vendor:
        match_status = "needs_vendor_review"
    elif po is None:
        match_status = "waiting_for_po"
    else:
        if po.vendor_id != vendor.id:
            match_issues.append("Vendor does not match purchase order")

        if not po.currency or not currency or po.currency.upper() != currency:
            match_issues.append("Currency does not match purchase order")

        if (
            po.total_amount is None
            or total_amount is None
            or abs(po.total_amount - total_amount) > 0.01
        ):
            match_issues.append("Invoice total does not match purchase order")

        match_status = "match_issues" if match_issues else "pending"

    invoice_values = {
        "invoice_no": invoice_no,
        "invoice_no_normalized": normalize_invoice_no(invoice_no),
        "vendor_id": vendor.id if vendor else None,
        "vendor_name_raw": vendor_name,
        "vendor_tax_id_raw": data.get("vendor_tax_id"),
        "invoice_date": _parse_document_date(data.get("invoice_date")),
        "due_date": _parse_document_date(data.get("due_date")),
        "currency": currency,
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "po_number_raw": po_number,
        "gl_code": str(data.get("gl_code") or "").strip()[:60] or None,
        "field_confidence": normalize_invoice_confidence(data),
        "po_id": po.id if po else None,
        "match_status": match_status,
    }

    if source_invoice is not None:
        if not source_invoice.is_current:
            return source_invoice
        unchanged = True
        for field, value in invoice_values.items():
            if field == "field_confidence":
                continue
            existing_value = getattr(source_invoice, field, None)
            if field == "invoice_no_normalized":
                # Derived keys may be absent on legacy rows and lightweight fixtures.
                existing_value = normalize_invoice_no(
                    getattr(source_invoice, "invoice_no", None)
                )
            if existing_value != value:
                unchanged = False
                break
        if not unchanged:
            raise ValueError(
                "Invoice extraction differs from the previously saved record; "
                "review required"
            )
        if "line_items" in data:
            stored_lines = (
                db.query(InvoiceLine)
                .filter(InvoiceLine.invoice_id == source_invoice.id)
                .all()
            )
            if _line_fingerprints(stored_lines) != _line_fingerprints(
                data["line_items"] or [], extracted=True
            ):
                raise ValueError(
                    "Invoice line extraction differs from the previously saved "
                    "record; review required"
                )
        source_invoice.field_confidence = invoice_values["field_confidence"]
        return source_invoice

    version_no = 1
    prior_versions = []

    if vendor is not None and invoice_no:
        normalized_no = invoice_values["invoice_no_normalized"]
        prior_versions = (
            db.query(Invoice)
            .filter(
                Invoice.vendor_id == vendor.id,
                Invoice.invoice_no_normalized == normalized_no,
            )
            .order_by(Invoice.version_no.desc())
            .with_for_update()
            .all()
        )

        if prior_versions:
            current_version = next(
                (item for item in prior_versions if getattr(item, "is_current", False)),
                None,
            ) or prior_versions[0]

            # Rule: If hold or active invoice, we cannot allow version two!
            # Only if the prior invoice was rejected is version two allowed.
            curr_status = getattr(current_version, "workflow_status", None) or getattr(current_version, "status", None)
            if curr_status != "rejected":
                raise DuplicateInvoiceError(
                    f"Duplicate invoice: {current_version.invoice_no} already exists with status '{curr_status}'. "
                    "New version (version 2) is only permitted if the prior invoice was rejected.",
                    original_invoice_id=current_version.id,
                )

            # Prior invoice was rejected -> Version two is allowed!
            version_no = max(invoice.version_no for invoice in prior_versions) + 1
            demoted_current = False
            for prior_invoice in prior_versions:
                if prior_invoice.is_current:
                    prior_invoice.is_current = False
                    demoted_current = True

            if demoted_current:
                db.flush()

    from src.services.invoice_issue_services import _assignment

    assigned_owner = _assignment(db)
    invoice = Invoice(
        source_document_id=document.id,
        owner_user_id=assigned_owner,
        created_by=getattr(document, "created_by", None),
        updated_by=getattr(document, "updated_by", None),
        version_no=version_no,
        is_current=True,
        **invoice_values,
    )

    db.add(invoice)
    db.flush()
    _replace_invoice_lines(db, invoice, data, po)

    return invoice


def _line_value(line: dict, *keys):
    return next((line.get(key) for key in keys if line.get(key) is not None), None)


_EXTRACT_UOM_PATTERN = (
    r"(?i)\b(PCS?|PIECES?|NOS?\.?|NUMBERS?|EA|EACH|UNITS?|"
    r"BOX(?:ES)?|PACK(?:S)?|PKTS?|SETS?|KGS?|KILOS?|GMS?|GRAMS?|"
    r"MTRS?|METERS?|LTRS?|LITRES?|DOZ(?:EN)?|HRS?|HOURS?|DAYS?|LOTS?|ROLLS?|BAGS?|PAIRS?|"
    r"CTN(?:S)?|CARTONS?|DRUMS?|CAN(?:S)?|BUNDLES?|"
    r"REAM(?:S)?|SHEETS?|PADS?|BOOKS?|BOTTLES?|PACKETS?|TINS?|BARRELS?|TONS?|TONNES?)\b"
)


def _extract_uom(raw_uom: Any, raw_qty: Any = None) -> str | None:
    if raw_uom and str(raw_uom).strip():
        return str(raw_uom).strip()[:10]
    if raw_qty and isinstance(raw_qty, str):
        match = re.search(_EXTRACT_UOM_PATTERN, raw_qty)
        if match:
            return match.group(1).upper()[:10]
    return None


def _apply_email_po_hint(document: Document, data: dict) -> None:
    """Fill a missing PO reference from email text without replacing extraction."""
    if data.get("order_number") or data.get("po_number"):
        return
    email_text = "\n".join(
        value
        for value in (
            getattr(document, "email_subject", None),
            getattr(document, "email_body_preview", None),
        )
        if value
    )
    match = re.search(
        r"\b(?:purchase\s+order|p\.?\s*o\.?)(?:\s*(?:number|no\.?|#))?\s*[:#-]?\s*"
        r"([A-Z0-9][A-Z0-9/_-]{2,})\b",
        email_text,
        re.IGNORECASE,
    )
    if match:
        data["order_number"] = match.group(1)
        data["order_number_source"] = "email"


def _document_failure_message(error: Exception, *, parse_failed: bool = False) -> str:
    failure_text = str(error).casefold()
    if "docx_parse_failed" in failure_text:
        return (
            "This Word document could not be read; save it as a new .docx file "
            "and upload it again."
        )
    if OCR_MODELS_UNAVAILABLE_MESSAGE.casefold() in failure_text:
        return OCR_MODELS_UNAVAILABLE_MESSAGE
    if any(
        phrase in failure_text
        for phrase in (
            "no text could be extracted",
            "no readable text",
            "empty document",
            "no text found",
        )
    ):
        if parse_failed:
            return "This file appears damaged; ask the vendor to resend it."
        return (
            "Scanned or image-only document with no readable text; "
            "upload an OCR-readable copy."
        )
    if any(
        phrase in failure_text
        for phrase in (
            "localentrynotfound",
            "huggingface",
            "model artifact",
            "model repository",
            "could not load model",
            "snapshot_download",
        )
    ):
        return OCR_MODELS_UNAVAILABLE_MESSAGE
    if any(
        phrase in failure_text
        for phrase in ("not an invoice", "not a bill", "document is not an invoice")
    ):
        return "This file does not appear to be an invoice; upload a bill to continue."
    if "new vendor:" in failure_text:
        return "New vendor. Add or select the vendor to continue matching."
    if any(word in failure_text for word in ("password", "encrypted", "not decrypted")):
        return "This file is password-protected; ask the vendor for an unlocked copy."
    if (
        isinstance(error, S3Error)
        or "storage" in failure_text
        or "minio" in failure_text
    ):
        return (
            "File storage is temporarily unavailable; retry document processing "
            "after storage connectivity is restored."
        )
    if any(
        marker in failure_text
        for marker in (
            "missing required field",
            "required field",
            "validation",
            "invalid monetary value",
            "invalid date",
            "line item",
            "line amounts do not reconcile",
            "amount mismatch",
        )
    ):
        missing_fields = re.findall(
            r"missing required field:\s*([a-z0-9_ -]+)", failure_text
        )
        fields = ", ".join(dict.fromkeys(field.strip() for field in missing_fields))
        if fields:
            return (
                f"Required document details are missing: {fields}. "
                "Review the highlighted fields and correct them."
            )
        return (
            "Some document details need review. Review the highlighted fields "
            "and correct them."
        )
    if parse_failed and any(
        marker in failure_text
        for marker in (
            "corrupt",
            "truncated",
            "unexpected eof",
            "format error",
            "invalid pdf",
            "invalid file format",
            "invalid document format",
            "invalid docx",
            "invalid zip archive",
        )
    ):
        return "This file appears damaged; ask the vendor to resend it."
    if "duplicate current invoice" in failure_text:
        return (
            "This invoice appears to be a duplicate; review the existing invoice "
            "before retrying."
        )
    if "line amounts do not reconcile" in failure_text:
        return (
            "Purchase order line amounts do not match its subtotal; correct the "
            "line values and retry."
        )
    if any(
        phrase in failure_text
        for phrase in ("unsupported image", "unsupported image format", "image format")
    ):
        return "This image format is unsupported; upload a PNG or JPG image."
    if any(
        word in failure_text
        for word in ("unreadable image", "image decode", "cannot identify image")
    ):
        return "This image could not be read; upload a clearer PNG or JPG image."
    if any(
        word in failure_text
        for word in ("unsupported", "not supported", "unknown format")
    ):
        return "This file type is unsupported; ask the vendor to send a PDF or image."
    if "vendor" in failure_text and any(
        word in failure_text for word in ("unknown", "not found", "unresolved")
    ):
        return (
            "The vendor could not be identified; verify the vendor details and retry."
        )
    if "goods receipt" in failure_text or "goods_receipt" in failure_text:
        return "The goods receipt could not be found; link a goods receipt and retry."
    if "purchase order" in failure_text or "po not found" in failure_text:
        return "The purchase order could not be found; link a purchase order and retry."
    if "currency" in failure_text:
        return (
            "The invoice and purchase order currencies do not match; "
            "correct the currency and retry."
        )
    if any(
        word in failure_text
        for word in (
            "timeout",
            "connection",
            "temporarily unavailable",
            "service unavailable",
            "rate limit",
        )
    ):
        return (
            "A document processing service is temporarily unavailable; "
            "retry processing shortly."
        )
    return (
        "Document extraction failed; retry processing. "
        "If it fails again, contact support."
    )


def _line_fingerprints(lines: list, *, extracted: bool = False) -> list[tuple]:
    def value(line, *keys):
        return _line_value(line, *keys) if extracted else getattr(line, keys[0], None)

    def decimal_key(raw):
        parsed = _to_decimal(raw)
        return str(parsed.normalize()) if parsed is not None else None

    fingerprints = []
    for line in lines:
        if extracted and not isinstance(line, dict):
            raise ValueError("Extracted line item must be an object")
        fingerprints.append(
            (
                _normalize_invoice_value(
                    value(line, "item_code", "sku", "product_code")
                ),
                _normalize_invoice_value(
                    value(line, "description", "item_description")
                ),
                _normalize_invoice_value(value(line, "uom", "unit_of_measure")),
                decimal_key(value(line, "qty", "quantity")),
                decimal_key(value(line, "unit_price", "price")),
                decimal_key(value(line, "line_amount", "amount", "total")),
            )
        )
    return sorted(fingerprints, key=repr)


def _replace_invoice_lines(
    db: Session, invoice: Invoice, data: dict, po: PurchaseOrder | None
) -> None:
    if "line_items" not in data:
        return
    db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).delete(
        synchronize_session=False
    )
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        if po
        else []
    )
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        item_code = _line_value(raw, "item_code", "sku", "product_code")
        description = _line_value(raw, "description", "item_description")
        candidates = [
            line
            for line in po_lines
            if (
                item_code
                and _normalize_invoice_value(line.item_code)
                == _normalize_invoice_value(item_code)
            )
            or (
                not item_code
                and description
                and _normalize_invoice_value(line.description)
                == _normalize_invoice_value(description)
            )
        ]
        db.add(
            InvoiceLine(
                invoice_id=invoice.id,
                item_code=str(item_code)[:60] if item_code else None,
                description=str(description)[:300] if description else None,
                uom=_extract_uom(
                    _line_value(raw, "uom", "unit_of_measure"),
                    _line_value(raw, "qty", "quantity"),
                ),
                qty=_to_decimal(_line_value(raw, "qty", "quantity")),
                unit_price=_to_decimal(_line_value(raw, "unit_price", "price")),
                line_amount=_to_decimal(
                    _line_value(raw, "line_amount", "amount", "total")
                ),
                field_confidence=(
                    normalize_invoice_confidence({"line_items": [raw]})["line_items"][0]
                ),
                po_line_id=candidates[0].id if len(candidates) == 1 else None,
            )
        )


class AwaitingRelatedDocumentError(ValueError):
    """The source document is valid, but a related record has not arrived."""


class AwaitingHumanDecisionError(ValueError):
    """Available identifiers resolve to more than one possible record."""


class DuplicateInvoiceError(ValueError):
    """The same current invoice content has already been processed."""

    def __init__(self, message: str, original_invoice_id=None):
        super().__init__(message)
        self.original_invoice_id = original_invoice_id


def _record_invoice_extraction_issues(db, invoice: Invoice, issues: list[str]) -> None:
    """Keep extraction validation findings visible beside the saved invoice."""
    active_fields = set()
    for issue in issues:
        normalized = issue.casefold()
        field = (
            issue.split(":", 1)[1].strip()
            if issue.startswith("Missing required field:")
            else "amounts"
            if "amount mismatch" in normalized or "monetary" in normalized
            else "extraction"
        )[:80]
        field_key = f"extraction_{field}"[:80]
        active_fields.add(field_key)
        discrepancy = (
            db.query(InvoiceDiscrepancy)
            .filter(
                InvoiceDiscrepancy.invoice_id == invoice.id,
                InvoiceDiscrepancy.field == field_key,
                InvoiceDiscrepancy.resolved.is_(False),
            )
            .first()
        )
        if discrepancy is None:
            discrepancy = InvoiceDiscrepancy(
                invoice_id=invoice.id,
                field=field_key,
                severity="error",
                resolved=False,
            )
            db.add(discrepancy)
        discrepancy.reason = issue
        discrepancy.invoice_value = {"extracted": True}
    for discrepancy in (
        db.query(InvoiceDiscrepancy)
        .filter(
            InvoiceDiscrepancy.invoice_id == invoice.id,
            InvoiceDiscrepancy.field.like("extraction_%"),
            InvoiceDiscrepancy.resolved.is_(False),
        )
        .all()
    ):
        if discrepancy.field not in active_fields:
            discrepancy.resolved = True


def _save_goods_receipt_from_extraction(
    db: Session,
    document: Document,
    data: dict,
    vendor: Vendor | None,
    *,
    purchase_order_id: uuid.UUID | None = None,
    validation_issues: list[str] | None = None,
) -> None:
    """Save a readable receipt even when PO/vendor/optional details need review."""
    gr_number = str(
        data.get("gr_number")
        or data.get("receipt_number")
        or data.get("goods_receipt_number")
        or ""
    ).strip()
    if not gr_number:
        raise ValueError("Goods receipt number is required")
    existing_receipt = (
        db.query(GoodsReceipt)
        .filter(GoodsReceipt.source_document_id == document.id)
        .first()
    )
    if existing_receipt is not None:
        existing_receipt.field_confidence = _extracted_field_confidence(data)
        existing_receipt.vendor_id = vendor.id if vendor else existing_receipt.vendor_id
    duplicate_query = db.query(GoodsReceipt).filter(
        func.lower(func.trim(GoodsReceipt.gr_number)) == gr_number.lower()
    )
    if existing_receipt is not None:
        duplicate_query = duplicate_query.filter(GoodsReceipt.id != existing_receipt.id)
    duplicate = duplicate_query.first()
    if duplicate:
        raise ValueError(f"Duplicate goods receipt number: {gr_number}")
    po_number = str(data.get("order_number") or data.get("po_number") or "").strip()
    if purchase_order_id is not None:
        selected_po = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.id == purchase_order_id,
                PurchaseOrder.is_current.is_(True),
            )
            .first()
        )
        po_candidates = (
            [selected_po]
            if selected_po is not None
            and _normalize_invoice_value(selected_po.po_number)
            == _normalize_invoice_value(po_number)
            else []
        )
    else:
        po_candidates = (
            db.query(PurchaseOrder)
            .filter(
                func.lower(func.trim(PurchaseOrder.po_number)) == po_number.lower(),
                PurchaseOrder.is_current.is_(True),
            )
            .all()
        )
    po = po_candidates[0] if len(po_candidates) == 1 else None
    if po and vendor and po.vendor_id and po.vendor_id != vendor.id:
        validation_issues = [*(validation_issues or []), "Vendor does not match PO"]
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        if po is not None
        else []
    )
    mapped = []
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        code = _line_value(raw, "item_code", "sku", "product_code")
        description = _line_value(raw, "description", "item_description")
        extracted_po_line_id = raw.get("po_line_id")
        candidates = []
        if extracted_po_line_id is not None:
            candidates = [
                line for line in po_lines if str(line.id) == str(extracted_po_line_id)
            ]
        else:
            candidates = [
                line
                for line in po_lines
                if code
                and _normalize_invoice_value(line.item_code)
                == _normalize_invoice_value(code)
            ]
            if not candidates and description:
                candidates = [
                    line
                    for line in po_lines
                    if _normalize_invoice_value(line.description)
                    == _normalize_invoice_value(description)
                ]
        po_line = candidates[0] if len(candidates) == 1 else None
        extracted_uom = _line_value(raw, "uom", "unit_of_measure")
        if (
            extracted_uom
            and po_line
            and po_line.uom
            and _normalize_invoice_value(extracted_uom)
            != _normalize_invoice_value(po_line.uom)
        ):
            raise ValueError("Goods receipt line unit does not match its PO line")
        raw_rec_qty = _line_value(raw, "received_qty", "quantity", "qty")
        extracted_uom = _extract_uom(_line_value(raw, "uom", "unit_of_measure"), raw_rec_qty)
        received = _to_decimal(raw_rec_qty)
        accepted = _to_decimal(raw.get("accepted_qty"))
        rejected = _to_decimal(raw.get("rejected_qty"))
        if accepted is None and len(data.get("line_items") or []) <= 1:
            accepted = _to_decimal(data.get("accepted_qty"))
        if accepted is None and received is not None:
            rejected_val = rejected or _to_decimal(data.get("rejected_qty")) or Decimal(0)
            accepted = max(Decimal(0), received - rejected_val)
        valid_received = received is not None and received > 0
        valid_accepted = accepted is not None and accepted >= 0
        valid_rejected = rejected is not None and rejected >= 0
        if (
            valid_received
            and valid_accepted
            and valid_rejected
            and accepted + rejected != received
        ):
            valid_accepted = valid_rejected = False
        mapped.append(
            GoodsReceiptLine(
                po_line_id=po_line.id if po_line else None,
                item_code=(
                    po_line.item_code if po_line else str(code)[:60] if code else None
                ),
                description=(
                    po_line.description
                    if po_line
                    else str(description)[:300]
                    if description
                    else None
                ),
                uom=str(extracted_uom)[:10] if extracted_uom else None,
                received_qty=received if valid_received else None,
                accepted_qty=accepted if valid_accepted else None,
                rejected_qty=rejected if valid_rejected else None,
                field_confidence=(
                    raw.get("field_confidence")
                    if isinstance(raw.get("field_confidence"), dict)
                    else {}
                ),
            )
        )
    receipt_date = _parse_document_date(data.get("receipt_date"))
    receipt = existing_receipt or GoodsReceipt(
        gr_number=gr_number,
        source_document_id=document.id,
    )
    receipt.po_id = po.id if po else receipt.po_id
    receipt.vendor_id = vendor.id if vendor else receipt.vendor_id
    receipt.receipt_date = receipt_date or receipt.receipt_date
    receipt.field_confidence = _extracted_field_confidence(data)
    # Confidence is informational for procurement records. Only concrete
    # validation/linking issues should require review; low confidence alone
    # must not block or flag a usable PO/GR record.
    receipt.needs_verification = (
        bool(validation_issues)
        or receipt.po_id is None
        or receipt.vendor_id is None
    )
    if mapped:
        receipt.lines = mapped
    if existing_receipt is None:
        db.add(receipt)
    db.flush()
    if po is not None:
        _rematch_waiting_invoices_for_po(db, po)
    else:
        raw_po_ref = data.get("po_number") or data.get("order_number")
        if raw_po_ref:
            matched_po = db.query(PurchaseOrder).filter(
                func.lower(func.trim(PurchaseOrder.po_number)) == _normalize_invoice_value(raw_po_ref),
                PurchaseOrder.is_current.is_(True),
            ).first()
            if matched_po:
                receipt.po_id = matched_po.id
                _rematch_waiting_invoices_for_po(db, matched_po)


def _is_invoice_version_conflict(exc: IntegrityError) -> bool:
    """Identify conflicts covered by the invoice version constraints."""
    constraint_name = getattr(
        getattr(exc.orig, "diag", None),
        "constraint_name",
        None,
    )

    if constraint_name in {
        "uq_invoice_version",
        "uq_current_vendor_invoice",
        "uq_current_vendor_invoice_normalized",
    }:
        return True

    error_message = str(exc.orig)
    return (
        "invoices.vendor_id, invoices.invoice_no, invoices.version_no" in error_message
        or "uq_invoice_version" in error_message
        or "uq_current_vendor_invoice" in error_message
        or "uq_current_vendor_invoice_normalized" in error_message
    )


def _rematch_waiting_invoices_for_po(
    db: Session,
    po: PurchaseOrder,
) -> int:
    """Rematch current invoices waiting for this purchase order."""
    po_number = _normalize_invoice_value(getattr(po, "po_number", None))
    if not po_number:
        return 0

    invoices = (
        db.query(Invoice)
        .filter(
            func.lower(func.trim(Invoice.po_number_raw)) == po_number,
            Invoice.is_current.is_(True),
        )
        .with_for_update()
        .all()
    )

    rematched = 0

    for invoice in invoices:
        final_run_id = getattr(invoice, "final_run_id", None)
        prior_run = db.get(MatchRun, final_run_id) if final_run_id else None
        saved_outcome = (
            (prior_run.rules_used or {}).get("match_outcome") if prior_run else None
        ) or {}
        saved_codes = {
            item.get("code")
            for item in saved_outcome.get("discrepancies", [])
            if isinstance(item, dict)
        }
        should_rematch = (
            invoice.match_status in {"waiting_for_po", "pending", "match_issues", "needs_vendor_review"}
            or invoice.status in {"processing", "needs_review"}
            or invoice.po_id is None
            or prior_run is None
            or bool(saved_codes.intersection({"MISSING_PO", "MISSING_GR", "QUANTITY_VARIANCE"}))
        )
        if not should_rematch:
            continue
        invoice.po_id = po.id
        if getattr(invoice, "id", None):
            run_match_for_invoice(
                db,
                invoice.id,
                getattr(invoice, "updated_by", None)
                or getattr(invoice, "created_by", None),
                force=True,
                idempotent=True,
                trigger="dependency_arrived",
            )
        rematched += 1

    db.flush()
    return rematched


def _save_pending_goods_receipts_for_po(db: Session, po: PurchaseOrder) -> int:
    """Attach previously extracted GR documents when their PO arrives later."""
    documents = (
        db.query(Document)
        .filter(
            Document.doc_type == "goods_receipt",
            Document.status.in_({"extracted", "needs_data_review"}),
            Document.extracted_json.isnot(None),
        )
        .all()
    )
    saved = 0
    for document in documents:
        data = document.extracted_json
        if not isinstance(data, dict):
            continue
        referenced_po = str(
            data.get("order_number") or data.get("po_number") or ""
        ).strip()
        if _normalize_invoice_value(referenced_po) != _normalize_invoice_value(
            po.po_number
        ):
            continue
        vendor_name = str(data.get("vendor") or "").strip()
        vendor = resolve_vendor(db, vendor_name, tax_id=data.get("vendor_tax_id"))
        if vendor is not None and po.vendor_id and vendor.id != po.vendor_id:
            continue
        try:
            _save_goods_receipt_from_extraction(
                db,
                document,
                data,
                vendor,
                purchase_order_id=po.id,
                validation_issues=(["New vendor"] if vendor is None else []),
            )
        except (
            AwaitingRelatedDocumentError,
            AwaitingHumanDecisionError,
            ValueError,
        ):
            logger.info(
                "Deferred goods receipt %s remains unresolved for PO %s",
                document.id,
                po.id,
            )
            continue
        saved += 1
    return saved


def _save_purchase_order_from_extraction(
    db: Session,
    document: Document,
    data: dict,
    vendor: Vendor | None,
    *,
    validation_issues: list[str] | None = None,
) -> PurchaseOrder:
    """Persist a readable PO; incomplete extracted fields remain reviewable."""
    po_date = data.get("po_date") or data.get("order_date")
    raw_currency = str(data.get("currency") or "").strip().upper()
    currency = raw_currency if re.fullmatch(r"[A-Z]{3}", raw_currency) else None
    total_amount = _to_decimal(data.get("total"))
    delivery_details = data.get("delivery_details")
    if delivery_details is None:
        delivery_details = {
            key: data[key]
            for key in (
                "delivery_address",
                "ship_to",
                "ship_to_address",
                "delivery_date",
                "expected_delivery_date",
                "delivery_method",
                "shipping_terms",
            )
            if data.get(key) is not None
        } or None
    existing = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.source_document_id == document.id)
        .first()
    )
    if existing is not None:
        if existing.vendor_id is None and vendor is not None:
            existing.vendor_id = vendor.id
        existing.field_confidence = _extracted_field_confidence(data)
        # Invoice confidence gates apply to invoices only. Procurement records
        # remain usable for matching even when extracted confidence is low.
        existing.needs_verification = bool(validation_issues)
        return existing

    purchase_order = PurchaseOrder(
        po_number=_purchase_order_number(data),
        vendor_id=vendor.id if vendor else None,
        source_document_id=document.id,
        po_date=_parse_document_date(po_date),
        currency=currency,
        subtotal=_to_decimal(data.get("subtotal")),
        tax_amount=_to_decimal(
            data.get("tax") if data.get("tax") is not None else data.get("tax_amount")
        ),
        tax_rate=_to_decimal(data.get("tax_rate")),
        total_amount=total_amount,
        payment_terms=data.get("payment_terms"),
        delivery_details=delivery_details,
        field_confidence=_extracted_field_confidence(data),
        needs_verification=bool(validation_issues),
        created_by=getattr(document, "created_by", None),
        updated_by=document.created_by,
    )

    db.add(purchase_order)
    db.flush()

    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        item_code = _line_value(raw, "item_code", "sku", "product_code")
        description = _line_value(raw, "description", "item_description")
        db.add(
            PurchaseOrderLine(
                po_id=purchase_order.id,
                item_code=str(item_code)[:60] if item_code else None,
                description=str(description)[:300] if description else None,
                uom=_extract_uom(
                    _line_value(raw, "uom", "unit_of_measure"),
                    _line_value(raw, "qty", "quantity"),
                ),
                qty=_to_decimal(_line_value(raw, "qty", "quantity")),
                unit_price=_to_decimal(_line_value(raw, "unit_price", "price")),
                line_amount=_to_decimal(
                    _line_value(raw, "line_amount", "amount", "total")
                ),
                field_confidence=(
                    raw.get("field_confidence")
                    if isinstance(raw.get("field_confidence"), dict)
                    else {}
                ),
                created_by=document.created_by,
                updated_by=document.created_by,
            )
        )

    _rematch_waiting_invoices_for_po(db, purchase_order)
    _save_pending_goods_receipts_for_po(db, purchase_order)

    return purchase_order


@router.post("/{document_id}/resolve-po-vendor")
def resolve_purchase_order_vendor(
    document_id: uuid.UUID,
    payload: PurchaseOrderVendorResolutionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_scope_filters(owner_id, current_user),
        )
        .with_for_update()
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.doc_type != "po" or not isinstance(document.extracted_json, dict):
        raise HTTPException(
            status_code=409,
            detail="A purchase order extraction is required before vendor review",
        )

    data = dict(document.extracted_json)
    existing = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.source_document_id == document.id)
        .first()
    )
    if existing is not None:
        is_same_vendor = payload.vendor_id == existing.vendor_id
        if payload.new_vendor is not None:
            existing_vendor = db.get(Vendor, existing.vendor_id)
            is_same_vendor = existing_vendor is not None and normalize_vendor_name(
                existing_vendor.name
            ) == normalize_vendor_name(payload.new_vendor.name)
        if not is_same_vendor:
            raise HTTPException(
                status_code=409,
                detail="This document already has a purchase order with another vendor",
            )
        return {
            "purchase_order": PurchaseOrderResponse.model_validate(existing).model_dump(
                mode="json"
            ),
            "document": DocumentSummary.model_validate(document).model_dump(
                mode="json"
            ),
        }

    if document.status not in {"needs_data_review", "needs_vendor_review", "extracted"}:
        raise HTTPException(
            status_code=409,
            detail="The document is not ready for purchase order vendor review",
        )

    if payload.vendor_id is not None:
        vendor = db.get(Vendor, payload.vendor_id)
        if vendor is None:
            raise HTTPException(status_code=404, detail="Vendor not found")
        if not vendor.is_active:
            raise HTTPException(status_code=409, detail="Vendor is inactive")
        new_vendor = None
    else:
        vendor = None
        new_vendor = payload.new_vendor
        if new_vendor is None:
            raise HTTPException(
                status_code=422,
                detail="Select an existing vendor or provide a new vendor",
            )
    selected_vendor_name = vendor.name if vendor is not None else new_vendor.name
    validation_data = dict(data)
    validation_data["vendor"] = selected_vendor_name
    if not validation_data.get("order_number"):
        validation_data["order_number"] = validation_data.get("po_number")
    if validation_data.get("tax") is None:
        validation_data["tax"] = validation_data.get("tax_amount")
    validation_issues = validate_extracted_data(validation_data, "po")
    validation_issues.extend(_purchase_order_persistence_issues(validation_data))
    if validation_issues:
        raise HTTPException(
            status_code=422,
            detail={"validation_issues": validation_issues},
        )

    duplicate = _find_duplicate_purchase_order(
        db,
        _purchase_order_number(validation_data),
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate current purchase order number: "
                f"{_purchase_order_number(validation_data)}"
            ),
        )

    if new_vendor is not None:
        if find_canonical_name_conflict(db, new_vendor.name) is not None:
            raise HTTPException(
                status_code=409,
                detail="A vendor with this name already exists; select that vendor",
            )
        if find_active_alias_conflict(db, new_vendor.name) is not None:
            raise HTTPException(
                status_code=409,
                detail="Vendor name conflicts with an existing vendor alias",
            )
        if new_vendor.tax_id:
            tax_id = re.sub(r"[^a-z0-9]", "", new_vendor.tax_id.casefold())
            existing_tax_id = (
                db.query(Vendor)
                .filter(
                    func.lower(
                        func.regexp_replace(Vendor.tax_id, r"[^[:alnum:]]", "", "g")
                    )
                    == tax_id
                )
                .first()
            )
            if existing_tax_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A vendor with this tax ID already exists; select that vendor"
                    ),
                )
        vendor = Vendor(
            name=new_vendor.name,
            tax_id=new_vendor.tax_id,
            contact_name=new_vendor.contact_name,
            contact_email=(
                str(new_vendor.contact_email) if new_vendor.contact_email else None
            ),
            contact_phone=new_vendor.contact_phone,
            created_by=owner_id,
            updated_by=owner_id,
        )
        db.add(vendor)
        db.flush()

    if vendor is None:
        raise HTTPException(status_code=422, detail="Vendor could not be resolved")

    extracted_vendor = data.get("vendor")
    resolved_data = dict(data)
    resolved_data["vendor"] = vendor.name
    resolved_data["vendor_resolution"] = {
        "method": "user_confirmed",
        "vendor_id": str(vendor.id),
        "extracted_value": extracted_vendor,
    }

    try:
        purchase_order = _save_purchase_order_from_extraction(
            db,
            document,
            resolved_data,
            vendor,
        )
        document.extracted_json = resolved_data
        document.status = "extracted"
        document.failure_message = None
        document.updated_by = owner_id
        db.add(
            AuditLog(
                action="purchase_order_vendor_resolved",
                entity_type="document",
                entity_id=document.id,
                old_value={"vendor": extracted_vendor},
                new_value={
                    "vendor_id": str(vendor.id),
                    "vendor_name": vendor.name,
                    "purchase_order_id": str(purchase_order.id),
                },
                message="PO vendor explicitly resolved and purchase order persisted",
                created_by=owner_id,
                updated_by=owner_id,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Purchase order or vendor conflicts with existing data",
        ) from exc

    db.refresh(purchase_order)
    db.refresh(document)
    try:
        refresh_related_workflows(db, document.id)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Workflow refresh failed after vendor resolution for %s (%s): %s",
            document.id,
            type(exc).__name__,
            _safe_exception_detail(exc),
        )

    return {
        "purchase_order": PurchaseOrderResponse.model_validate(
            purchase_order
        ).model_dump(mode="json"),
        "document": DocumentSummary.model_validate(document).model_dump(mode="json"),
    }


@router.post("/{document_id}/parse", dependencies=[Depends(get_current_user)])
def parse_document(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    worker_call = current_user.get("worker") is True
    owner_id = None if worker_call else _document_owner_id(current_user)
    if worker_call:
        query = db.query(Document).filter(Document.id == document_id)
    else:
        query = db.query(Document).filter(
            Document.id == document_id, *_document_owner_filters(owner_id)
        )
    document = query.with_for_update().first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if not document.storage_path:
        raise HTTPException(
            status_code=404,
            detail="Stored file not found",
        )

    if getattr(document, "source_flag", None) == "bulk_sender":
        document.doc_type = "other"
        try:
            _route_document_to_other(db, document, "other", 1.0)
            document.failure_message = "bulk sender, not a bill"
            db.commit()
        except Exception as exc:
            db.rollback()
            document.failure_message = (
                "Bulk sender attachment could not be moved to Other storage; "
                f"retry processing ({type(exc).__name__})."
            )[:500]
            db.commit()
        return {
            "id": str(document.id),
            "status": document.status,
            "doc_type": "other",
            "failure_message": document.failure_message,
        }

    extension = Path(document.original_filename).suffix.casefold()
    if extension not in ALLOWED_EXTENSIONS:
        document.status = "failed"
        document.failure_class = "permanent"
        document.next_retry_at = None
        document.failure_message = (
            "This file type is unsupported; ask the vendor to send a PDF or image."
        )
        if (
            may_send_automatic_reply(document)
            and getattr(document, "doc_type", None) != "other"
            and getattr(document, "email_sender", None)
        ):
            db.add(
                Notification(
                    document_id=document.id,
                    recipient_email=document.email_sender,
                    subject="Document could not be processed",
                    body=document.failure_message,
                    status="queued",
                )
            )
        db.commit()
        dispatch_queued_notifications(db, document.id, background_tasks)
        return {
            "id": str(document.id),
            "status": document.status,
            "failure_message": document.failure_message,
        }

    if document.status == "extracted":
        return {
            "id": str(document.id),
            "status": document.status,
            "doc_type": document.doc_type,
            "extracted_json": document.extracted_json,
            "validation_issues": [],
            "duplicate_of": None,
        }
    if document.status == "parsing":
        raise HTTPException(
            status_code=409, detail="Document processing is already in progress"
        )

    document.status = "parsing"
    db.commit()

    try:
        content = with_transient_retries(lambda: download_file(document.storage_path))

    except S3Error as exc:
        if exc.code in {
            "NoSuchKey",
            "NoSuchObject",
            "NoSuchBucket",
        }:
            document.status = "failed"
            document.failure_class = "permanent"
            document.next_retry_at = None
            document.failure_message = (
                "The stored file could not be found; upload it again."
            )
            db.commit()
            raise HTTPException(
                status_code=404,
                detail=document.failure_message,
            ) from None

        logger.error(
            "MinIO download failed (%s): %s",
            type(exc).__name__,
            _safe_exception_detail(exc),
        )
        document.status = "failed"
        document.failure_class = classify_document_failure(exc)
        document.next_retry_at = None
        document.failure_message = _document_failure_message(exc)
        db.commit()

        raise HTTPException(
            status_code=503,
            detail=document.failure_message,
        ) from None

    except Exception as exc:
        logger.error(
            "MinIO download failed (%s): %s",
            type(exc).__name__,
            _safe_exception_detail(exc),
        )
        document.status = "failed"
        document.failure_class = classify_document_failure(exc)
        document.next_retry_at = None
        document.failure_message = _document_failure_message(exc)
        db.commit()

        raise HTTPException(
            status_code=503,
            detail=document.failure_message,
        ) from None

    temp_path = None
    active_pipeline_step = "parse"
    active_step_started = time.perf_counter()

    try:
        # Step 1: Prepare the document.
        with tempfile.NamedTemporaryFile(
            suffix=Path(document.original_filename).suffix.lower(),
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        document.failure_message = None

        # Step 2: Parse using Docling.
        parse_started = time.perf_counter()
        if os.getenv("AP_E2E_MOCK_EXTRACTION") or is_e2e_stub_enabled():
            raw_text = extract_e2e_test_document_text(content)
        else:
            document_converter = importlib.import_module(
                "docling.document_converter"
            ).DocumentConverter

            converter = _get_converter(document_converter)
            try:
                result = converter.convert(temp_path)
                raw_text = result.document.export_to_markdown()
            except Exception as exc:
                if Path(temp_path).suffix.casefold() == ".docx":
                    raise ValueError("DOCX_PARSE_FAILED") from None
                models_missing = _docling_models_missing()
                if Path(temp_path).suffix.casefold() == ".pdf" and (
                    _is_extraction_model_error(exc) or models_missing
                ):
                    raw_text = _extract_pdf_text_layer(temp_path)
                    if not raw_text.strip():
                        if models_missing:
                            raise RuntimeError(OCR_MODELS_UNAVAILABLE_MESSAGE) from exc
                        raise
                    logger.warning(
                        "Used the embedded PDF text layer because extraction models "
                        "were unavailable (%s)",
                        _safe_exception_detail(exc),
                    )
                else:
                    if models_missing:
                        raise RuntimeError(OCR_MODELS_UNAVAILABLE_MESSAGE) from exc
                    raise

        if not raw_text or not raw_text.strip():
            if Path(temp_path).suffix.casefold() == ".docx":
                raise ValueError("DOCX_PARSE_FAILED")
            raise ValueError("No text could be extracted from the document")

        document.raw_text = raw_text
        document.status = "parsed"
        _record_pipeline_step(
            db,
            document,
            "parse",
            parse_started,
            output_summary={"characters": len(raw_text)},
        )
        db.commit()

        # Step 3: Classify using OpenRouter.
        active_pipeline_step = "classify"
        active_step_started = time.perf_counter()
        classify_started = time.perf_counter()
        classify_override = getattr(document, "classify_override", None)
        if classify_override:
            doc_type, classify_confidence = classify_override, 1.0
            document.classify_override = None
        else:
            cheap_result = _classify_document_cheaply(
                document.original_filename, raw_text
            )
            if cheap_result is not None:
                doc_type, classify_confidence = cheap_result
            else:
                try:
                    supports_confidence = (
                        "with_confidence"
                        in inspect.signature(classify_document).parameters
                    )
                    if supports_confidence:
                        classified = with_transient_retries(
                            lambda: classify_document(raw_text, with_confidence=True)
                        )
                    else:
                        classified = with_transient_retries(
                            lambda: classify_document(raw_text)
                        )
                    if isinstance(classified, tuple):
                        doc_type, classify_confidence = classified
                    else:
                        doc_type, classify_confidence = classified, 1.0
                except Exception as exc:
                    if is_transient_document_error(exc):
                        raise
                    logger.warning(
                        "Document classification failed for %s (%s)",
                        document.id,
                        type(exc).__name__,
                    )
                    doc_type, classify_confidence = "other", 0.0

        if doc_type == "gr":
            doc_type = "goods_receipt"
        try:
            classify_confidence = float(classify_confidence)
        except (TypeError, ValueError):
            classify_confidence = 0.0
        if not math.isfinite(classify_confidence):
            classify_confidence = 0.0
        try:
            min_confidence = float(os.getenv("DOC_CLASSIFY_MIN_CONFIDENCE", "0.70"))
        except (TypeError, ValueError):
            min_confidence = 0.70
        allowed_types = {"invoice", "po", "goods_receipt", "other"}
        if (
            not isinstance(doc_type, str)
            or doc_type not in allowed_types
            or classify_confidence < min_confidence
        ):
            doc_type = "other"

        document.doc_type = doc_type
        document.classify_confidence = classify_confidence
        _record_pipeline_step(
            db,
            document,
            "classify",
            classify_started,
            output_summary={"doc_type": doc_type, "confidence": classify_confidence},
        )
        if doc_type == "other":
            try:
                _route_document_to_other(db, document, doc_type, classify_confidence)
            except Exception as exc:
                db.rollback()
                document.doc_type = "other"
                document.classify_confidence = classify_confidence
                document.status = "failed"
                document.failure_class = classify_document_failure(exc)
                document.next_retry_at = None
                document.failure_message = _document_failure_message(exc)
                db.commit()
                return {
                    "id": str(document.id),
                    "status": document.status,
                    "doc_type": document.doc_type,
                    "failure_message": document.failure_message,
                }
            return {
                "id": str(document.id),
                "status": "other",
                "doc_type": doc_type,
                "classify_confidence": classify_confidence,
                "extracted_json": None,
                "validation_issues": [],
                "duplicate_of": None,
            }

        route_date = (
            getattr(document, "email_received_at", None)
            or getattr(document, "created_at", None)
            or datetime.now(UTC)
        )
        routed_key = (
            f"{doc_type}/{route_date:%Y}/{route_date:%m}/{document.id}/"
            f"{Path(document.original_filename).name}"
        )
        try:
            if document.storage_path and document.storage_path != routed_key:
                move_file(document.storage_path, routed_key)
        except Exception as exc:
            db.rollback()
            document.doc_type = doc_type
            document.classify_confidence = classify_confidence
            document.status = "failed"
            document.failure_class = classify_document_failure(exc)
            document.next_retry_at = None
            document.failure_message = _document_failure_message(exc)
            db.commit()
            return {
                "id": str(document.id),
                "status": document.status,
                "doc_type": document.doc_type,
                "failure_message": document.failure_message,
            }
        document.storage_path = routed_key
        document.status = "classified"
        db.commit()

        # Step 4: Extract fields using the configured LLM pipeline.
        active_pipeline_step = "extract"
        active_step_started = time.perf_counter()
        extract_started = time.perf_counter()
        extracted_data = with_transient_retries(
            lambda: extract_document(raw_text, doc_type)
        )
        if doc_type == "invoice" and isinstance(extracted_data, dict):
            _apply_email_po_hint(document, extracted_data)

        validation_data = extracted_data
        _record_pipeline_step(
            db,
            document,
            "extract",
            extract_started,
            output_summary={
                "field_count": len(extracted_data)
                if isinstance(extracted_data, dict)
                else 0
            },
        )

        if doc_type == "po" and isinstance(extracted_data, dict):
            validation_data = dict(extracted_data)

            if not validation_data.get("order_number"):
                validation_data["order_number"] = validation_data.get("po_number")

            if (
                validation_data.get("tax") is None
                and validation_data.get("tax_amount") is not None
            ):
                validation_data["tax"] = validation_data["tax_amount"]

        if doc_type == "goods_receipt" and isinstance(extracted_data, dict):
            validation_data = dict(extracted_data)
            validation_data["gr_number"] = (
                validation_data.get("gr_number")
                or validation_data.get("receipt_number")
                or validation_data.get("goods_receipt_number")
            )
            validation_data["order_number"] = validation_data.get(
                "order_number"
            ) or validation_data.get("po_number")
            gr_date = validation_data.get("receipt_date")
            if gr_date and _parse_document_date(gr_date) is None:
                validation_data["receipt_date"] = None
            for index, line in enumerate(validation_data.get("line_items") or []):
                if not isinstance(line, dict):
                    continue
                received = _to_decimal(
                    _line_value(line, "received_qty", "quantity", "qty")
                )
                accepted = _to_decimal(line.get("accepted_qty"))
                rejected = _to_decimal(line.get("rejected_qty"))
                if (
                    received is None
                    or accepted is None
                    or rejected is None
                    or received <= 0
                    or accepted < 0
                    or rejected < 0
                    or accepted + rejected != received
                ):
                    validation_data.setdefault("line_errors", []).append(
                        f"Line item {index + 1} has missing or invalid "
                        "received/accepted/rejected quantities"
                    )

        # Step 5: Validate the extracted fields.
        validation_issues = validate_extracted_data(
            validation_data,
            doc_type,
        )

        po_vendor = None
        goods_receipt_vendor = None
        vendor_resolve_started = time.perf_counter()

        if doc_type == "po" and isinstance(extracted_data, dict):
            validation_issues.extend(_purchase_order_persistence_issues(extracted_data))

            vendor_name = str(extracted_data.get("vendor") or "").strip()

            vendor_tax_id = str(extracted_data.get("vendor_tax_id") or "").strip()
            if vendor_name or vendor_tax_id:
                po_vendor = resolve_vendor(
                    db, vendor_name, tax_id=extracted_data.get("vendor_tax_id")
                )

                if po_vendor is None:
                    validation_issues.append(f"New vendor: {vendor_name}")

        if doc_type == "goods_receipt" and isinstance(extracted_data, dict):
            if validation_data.get("line_errors"):
                validation_issues.extend(validation_data["line_errors"])
            vendor_name = str(extracted_data.get("vendor") or "").strip()
            vendor_tax_id = str(extracted_data.get("vendor_tax_id") or "").strip()
            if vendor_name or vendor_tax_id:
                goods_receipt_vendor = resolve_vendor(
                    db, vendor_name, tax_id=extracted_data.get("vendor_tax_id")
                )
                if goods_receipt_vendor is None:
                    validation_issues.append(f"New vendor: {vendor_name}")
        _record_pipeline_step(
            db,
            document,
            "vendor_resolve",
            vendor_resolve_started,
            output_summary={"resolved": bool(po_vendor or goods_receipt_vendor)},
            result="success" if po_vendor or goods_receipt_vendor else "warning",
        )

        # Step 6: Check current purchase order duplicates.
        duplicate_purchase_order = None

        if (
            doc_type == "po"
            and isinstance(extracted_data, dict)
            and _purchase_order_number(extracted_data)
        ):
            duplicate_purchase_order = _find_duplicate_purchase_order(
                db,
                _purchase_order_number(extracted_data),
                exclude_document_id=document.id,
            )

        # Preserve extracted data for review.
        document.extracted_json = extracted_data

        # Persist extracted invoices even when fields need human correction.
        duplicate_invoice_id = None
        saved_invoice = None
        if doc_type == "invoice" and isinstance(extracted_data, dict):
            try:
                saved_invoice = _save_invoice_from_extraction(
                    db,
                    document,
                    extracted_data,
                )
                if saved_invoice is not None and validation_issues:
                    _record_invoice_extraction_issues(
                        db, saved_invoice, validation_issues
                    )
                    saved_invoice.status = "needs_review"
                if saved_invoice is not None:
                    run_match_for_invoice(
                        db, saved_invoice.id, getattr(document, "created_by", None), force=True
                    )
                    if requires_verification(
                        getattr(saved_invoice, "field_confidence", None) or {}
                    ):
                        if legacy_workflow_status(saved_invoice) not in {"verifying", "match_approved", "ready_to_pay"}:
                            transition_invoice(
                                db,
                                saved_invoice,
                                "verifying",
                                reason=(
                                    "Confidence unavailable, please verify"
                                    if confidence_unavailable(
                                        getattr(saved_invoice, "field_confidence", None)
                                        or {}
                                    )
                                    else (
                                        "One or more extracted fields need associate "
                                        "verification"
                                    )
                                ),
                            )

            except IntegrityError as exc:
                if not _is_invoice_version_conflict(exc):
                    raise

                db.rollback()

                document = db.query(Document).filter_by(id=document_id).first()

                if document is None:
                    raise RuntimeError(
                        "Document disappeared during invoice versioning"
                    ) from exc

                document.extracted_json = extracted_data
                constraint_name = getattr(
                    getattr(exc.orig, "diag", None), "constraint_name", None
                )
                duplicate = None
                if constraint_name == "uq_current_vendor_invoice_normalized":
                    raced_vendor = resolve_vendor(
                        db,
                        str(extracted_data.get("vendor") or "").strip() or None,
                        tax_id=extracted_data.get("vendor_tax_id"),
                    )
                    duplicate = find_duplicate_invoice(
                        db,
                        raced_vendor.id if raced_vendor else None,
                        normalize_invoice_no(extracted_data.get("invoice_number")),
                    )
                if duplicate is not None:
                    duplicate_invoice_id = duplicate.id
                    validation_issues.append(
                        "Duplicate current invoice: same vendor and normalized number"
                    )
                else:
                    validation_issues.append(
                        "Invoice version conflict; document requires review"
                    )
            except DuplicateInvoiceError as exc:
                validation_issues.append(str(exc))
                duplicate_invoice_id = exc.original_invoice_id
                document.extracted_json = {
                    **extracted_data,
                    **(
                        {"duplicate_of_invoice_id": str(duplicate_invoice_id)}
                        if duplicate_invoice_id
                        else {}
                    ),
                }
            except ValueError as exc:
                validation_issues.append(str(exc))
        po_vendor_identity = bool(
            isinstance(extracted_data, dict)
            and (
                str(
                    extracted_data.get("vendor")
                    or extracted_data.get("vendor_name")
                    or ""
                ).strip()
                or str(
                    extracted_data.get("vendor_tax_id")
                    or extracted_data.get("tax_id")
                    or ""
                ).strip()
            )
        )
        if doc_type == "po" and _purchase_order_number(extracted_data):
            if duplicate_purchase_order is not None:
                validation_issues.append(
                    "Duplicate current purchase order number: "
                    f"{_purchase_order_number(extracted_data)}"
                )

            else:
                try:
                    _save_purchase_order_from_extraction(
                        db,
                        document,
                        extracted_data,
                        po_vendor,
                        validation_issues=validation_issues,
                    )
                except ValueError as exc:
                    validation_issues.append(str(exc))
                except IntegrityError as exc:
                    db.rollback()

                    duplicate_purchase_order = _find_duplicate_purchase_order(
                        db,
                        _purchase_order_number(extracted_data),
                        exclude_document_id=document.id,
                    )

                    if duplicate_purchase_order is None:
                        raise

                    document = db.query(Document).filter_by(id=document_id).first()

                    if document is None:
                        raise RuntimeError(
                            "Document disappeared during PO persistence"
                        ) from exc

                    document.extracted_json = extracted_data
                    validation_issues.append(
                        "Duplicate current purchase order number: "
                        f"{_purchase_order_number(extracted_data)}"
                    )

        if (
            doc_type == "goods_receipt"
            and isinstance(extracted_data, dict)
            and bool(
                extracted_data.get("gr_number")
                or extracted_data.get("receipt_number")
                or extracted_data.get("goods_receipt_number")
            )
        ):
            try:
                _save_goods_receipt_from_extraction(
                    db,
                    document,
                    extracted_data,
                    goods_receipt_vendor,
                    validation_issues=validation_issues,
                )
            except (AwaitingRelatedDocumentError, AwaitingHumanDecisionError):
                # Keep the validated source document available for later linking.
                pass
            except ValueError as exc:
                validation_issues.append(str(exc))

        # Step 7: Set final document status.
        if doc_type in {"po", "purchase_order", "goods_receipt"} and not any(
            issue.startswith("Duplicate") for issue in validation_issues
        ):
            document.status = "processed"
            document.failure_class = None
            document.failure_message = None
            document.next_retry_at = None
        elif validation_issues:
            duplicate_invoice = any(
                issue.startswith("Duplicate current invoice:")
                for issue in validation_issues
            )
            document.status = (
                "duplicate_blocked" if duplicate_invoice else "needs_data_review"
            )
            document.failure_class = "review"
            document.next_retry_at = None
            document.failure_message = _document_failure_message(
                ValueError(
                    next(
                        (
                            issue
                            for issue in validation_issues
                            if issue.startswith("New vendor:")
                        ),
                        "; ".join(validation_issues),
                    )
                )
            )
            if duplicate_invoice_id:
                document.extracted_json = {
                    **(document.extracted_json or {}),
                    "duplicate_of_invoice_id": str(duplicate_invoice_id),
                }

            logger.warning(
                "Document %s requires review: %s",
                document_id,
                validation_issues,
            )
            if (
                may_send_automatic_reply(document)
                and getattr(document, "doc_type", None) != "other"
                and getattr(document, "email_sender", None)
            ):
                db.add(
                    Notification(
                        document_id=document.id,
                        recipient_email=document.email_sender,
                        subject="Document needs attention",
                        body=document.failure_message,
                        status="queued",
                    )
                )

        else:
            document.status = "extracted"
            document.failure_class = None
            document.retry_count = 0
            document.next_retry_at = None
            document.failure_message = None

        db.commit()
        db.refresh(document)
        if isinstance(db, Session):
            try:
                refresh_related_workflows(db, document.id)
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.error(
                    "Workflow refresh failed after document %s was extracted (%s): %s",
                    document_id,
                    type(exc).__name__,
                    _safe_exception_detail(exc),
                )
        notify_started = time.perf_counter()
        dispatch_queued_notifications(db, document.id, background_tasks)
        _record_pipeline_step(
            db,
            document,
            "notify",
            notify_started,
            output_summary={"queued": True},
        )
        db.commit()

        # Step 8: Return extraction and duplicate-check results.
        return {
            "id": str(document.id),
            "status": document.status,
            "doc_type": document.doc_type,
            "extracted_json": document.extracted_json,
            "validation_issues": validation_issues,
            "duplicate_of": (
                str(duplicate_invoice_id)
                if duplicate_invoice_id
                else str(duplicate_purchase_order.id)
                if duplicate_purchase_order is not None
                else None
            ),
        }

    except Exception as exc:
        logger.error(
            "Document parse/classification/extraction failed for %s (%s): %s",
            document_id,
            type(exc).__name__,
            _safe_exception_detail(exc),
        )

        db.rollback()
        failure_detail = _document_failure_message(
            exc, parse_failed=active_pipeline_step == "parse"
        )

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document:
                db.add(
                    AuditLog(
                        action="pipeline_step",
                        entity_type="document",
                        entity_id=document.id,
                        step_name=active_pipeline_step,
                        duration_ms=max(
                            0,
                            int((time.perf_counter() - active_step_started) * 1000),
                        ),
                        input_ref=(document.storage_path or str(document.id))[:500],
                        output_summary={"error_type": type(exc).__name__},
                        result="error",
                        message=f"Document {active_pipeline_step} step failed",
                        created_by=getattr(document, "created_by", None),
                    )
                )
                document.status = "failed"
                document.failure_class = classify_document_failure(exc)
                document.next_retry_at = None
                document.failure_message = _document_failure_message(
                    exc, parse_failed=active_pipeline_step == "parse"
                )
                failure_detail = (
                    f"{document.original_filename}: {document.failure_message}"
                )
                if (
                    may_send_automatic_reply(document)
                    and getattr(document, "doc_type", None) != "other"
                    and getattr(document, "email_sender", None)
                ):
                    db.add(
                        Notification(
                            document_id=document.id,
                            recipient_email=document.email_sender,
                            subject="Document could not be processed",
                            body=document.failure_message,
                            status="queued",
                        )
                    )
                db.commit()
                dispatch_queued_notifications(db, document.id)

        except Exception as status_error:
            db.rollback()
            logger.error(
                "Failed to update document failure status (%s): %s",
                type(status_error).__name__,
                _safe_exception_detail(status_error),
            )

        raise HTTPException(
            status_code=500,
            detail=failure_detail,
        ) from None

    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


@router.get("/{document_id}/file")
def get_document_file(
    document_id: uuid.UUID,
    disposition: Literal["inline", "attachment"] = Query(default="attachment"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_scope_filters(_document_owner_id(current_user), current_user),
        )
        .first()
    )

    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if not document.storage_path:
        raise HTTPException(status_code=404, detail="Original file is unavailable")
    try:
        content = download_file(document.storage_path)
    except Exception as exc:
        logger.error(
            "MinIO file download failed (%s): %s",
            type(exc).__name__,
            _safe_exception_detail(exc),
        )
        raise HTTPException(
            status_code=502, detail="Original file could not be retrieved"
        ) from exc
    extension = Path(document.original_filename).suffix.lower()
    media_type = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        ".eml": "message/rfc822",
        ".txt": "text/plain",
    }.get(extension, "application/octet-stream")
    filename = quote(document.original_filename, safe="")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{document_id}/preview")
def get_document_preview(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Return an inline preview without replacing or exposing the original object."""
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_scope_filters(_document_owner_id(current_user), current_user),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    extension = Path(document.original_filename).suffix.casefold()
    if extension in {".pdf", ".png", ".jpg", ".jpeg"}:
        if not document.storage_path:
            raise HTTPException(status_code=404, detail="Original file is unavailable")
        try:
            content = download_file(document.storage_path)
        except Exception as exc:
            logger.error("Preview source download failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=502, detail="Document preview could not be retrieved"
            ) from exc
        media_type = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }[extension]
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if extension != ".docx":
        raise HTTPException(
            status_code=415,
            detail={
                "code": "preview_unsupported",
                "message": "This file type does not support a preview.",
            },
        )
    try:
        content = get_docx_preview(document)
    except DocumentPreviewError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{document_id}/rerun", status_code=202)
@router.post("/{document_id}/retry", status_code=202)
def retry_document_processing(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_scope_filters(owner_id, current_user),
        )
        .with_for_update()
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    stale_parse = (
        document.status == "parsing"
        and isinstance(document.updated_at, datetime)
        and document.updated_at.tzinfo is not None
        and document.updated_at <= datetime.now(UTC) - STALE_PARSE_TIMEOUT
    )
    if (
        document.status
        not in {"failed", "needs_data_review", "needs_vendor_review", "incomplete"}
        and not stale_parse
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Only failed, review-required, or stale processing documents "
                "can be retried"
            ),
        )
    old_status = document.status
    is_raw_email = (
        getattr(document, "source", None) == "email"
        and getattr(document, "parent_document_id", None) is None
        and (
            getattr(document, "original_filename", "").casefold().endswith(".eml")
            or (getattr(document, "mime", None) or "").casefold() == "message/rfc822"
        )
    )
    retry_status = "parsing" if is_raw_email else "received"
    document.status = retry_status
    document.failure_message = None
    document.failure_class = None
    document.retry_count = 0
    document.next_retry_at = None
    actor_id = owner_id
    document.updated_by = actor_id
    db.add(
        AuditLog(
            action="document_processing_retry_requested",
            entity_type="document",
            entity_id=document.id,
            old_value={"status": old_status},
            new_value={"status": retry_status},
            message="Document processing retry requested",
            created_by=actor_id,
        )
    )
    db.commit()
    if is_raw_email:
        dispatch_raw_email_recovery(background_tasks, document.id)
    else:
        dispatch_document_processing(
            background_tasks,
            document.id,
            _process_document_with_retries,
        )
    return {"id": str(document.id), "status": document.status}
