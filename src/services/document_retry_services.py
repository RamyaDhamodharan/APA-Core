"""Failure classification and bounded retries for document processing."""

import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from src.db.session_db import SessionLocal
from src.models.audit_models import AuditLog
from src.models.document_models import Document

logger = logging.getLogger(__name__)
RETRY_DELAYS_SECONDS = (30, 120, 600)
MAX_AUTOMATIC_RETRIES = len(RETRY_DELAYS_SECONDS)
PERMANENT_MARKERS = (
    "corrupt",
    "password-protected",
    "password protected",
    "encrypted",
    "unsupported",
    "no text could be extracted",
    "empty document",
    "no text found",
    "invalid pdf",
    "invalid file format",
    "invalid document format",
    "docx_parse_failed",
)
TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "redis",
    "database",
    "operationalerror",
    "disconnectionerror",
    "storage",
    "minio",
    "temporarily unavailable",
    "service unavailable",
    "model unavailable",
    "provider unavailable",
    "inference unavailable",
    "rate limit",
    "too many requests",
    "worker lost",
    "worker crash",
    " 429",
    " 500",
    " 502",
    " 503",
    " 504",
)


def classify_document_failure(error: BaseException) -> str:
    """Classify known permanent file failures before transient infrastructure errors."""
    parts = []
    current: BaseException | None = error
    while current is not None:
        parts.extend((type(current).__name__.casefold(), str(current).casefold()))
        current = current.__cause__ or current.__context__
    detail = " ".join(parts)
    if any(marker in detail for marker in PERMANENT_MARKERS):
        return "permanent"
    if any(marker in detail for marker in TRANSIENT_MARKERS):
        return "retryable"
    # Unclassified errors are not safe to replay automatically. Worker crashes are
    # identified independently by the stale-job sweeper.
    return "permanent"


def retry_delay_seconds(retry_count: int) -> int:
    return RETRY_DELAYS_SECONDS[min(max(retry_count, 0), MAX_AUTOMATIC_RETRIES - 1)]


def is_transient_document_error(error: BaseException) -> bool:
    return classify_document_failure(error) == "retryable"


def with_transient_retries(
    operation: Callable[[], object], *, sleep: Callable[[float], None] = time.sleep
):
    """Run a single attempt; retries are managed and recorded per document."""
    del sleep
    return operation()


def _attempt_audit(
    db,
    document: Document,
    *,
    attempt: int,
    result: str,
    reason: str | None = None,
    next_retry_at: datetime | None = None,
    actor_id=None,
) -> None:
    phase = "started" if result == "warning" and reason is None else result
    try:
        with db.begin_nested():
            db.add(
                AuditLog(
                    action="document_processing_attempt",
                    entity_type="document",
                    entity_id=document.id,
                    result=result,
                    message=f"Document processing attempt {attempt} {phase}",
                    reason=reason,
                    new_value={
                        "attempt": attempt,
                        "maximum_retries": MAX_AUTOMATIC_RETRIES,
                        "failure_class": document.failure_class,
                        "next_retry_at": next_retry_at.isoformat()
                        if next_retry_at
                        else None,
                        "phase": phase,
                    },
                    created_by=actor_id,
                )
            )
            db.flush()
    except Exception as exc:
        logger.warning("Document attempt audit write failed (%s)", type(exc).__name__)


def _mark_unexpected_failure(
    make_session: Callable,
    document_id: uuid.UUID,
    attempt_number: int,
    error: BaseException,
) -> None:
    """Ensure an unexpected worker exception cannot leave a document processing."""
    db = make_session()
    try:
        document = (
            db.query(Document)
            .filter(Document.id == document_id)
            .with_for_update()
            .first()
        )
        if document is None or document.status in {"failed", "processed", "extracted"}:
            return
        document.status = "failed"
        document.failure_class = classify_document_failure(error)
        document.next_retry_at = None
        document.failure_message = (
            "Document processing stopped unexpectedly. Retry the document or "
            "upload the file again."
        )
        _attempt_audit(
            db,
            document,
            attempt=attempt_number,
            result="error",
            reason=document.failure_message,
            actor_id=getattr(document, "updated_by", None)
            or getattr(document, "created_by", None),
        )
        db.commit()
    except Exception as exc:
        logger.warning(
            "Unable to mark document attempt failed (%s)", type(exc).__name__
        )
    finally:
        db.close()


def process_document_attempt(
    document_id: uuid.UUID,
    *,
    session_factory: Callable | None = None,
) -> int | None:
    """Run once; return delay seconds when a retry was scheduled."""
    from src.api.rest.routers.document_routers import _process_in_background

    make_session = session_factory or SessionLocal
    db = make_session()
    try:
        document = (
            db.query(Document)
            .filter(Document.id == document_id)
            .with_for_update()
            .first()
        )
        if document is None:
            return None
        retry_index = int(document.retry_count or 0)
        if document.status == "failed" and document.failure_class == "retryable":
            document.status = "received"
            document.next_retry_at = None
            document.failure_message = None
        if document.status != "received":
            return None
        document.last_attempt_at = datetime.now(UTC)
        attempt_number = retry_index + 1
        _attempt_audit(
            db,
            document,
            attempt=attempt_number,
            result="warning",
            actor_id=getattr(document, "updated_by", None)
            or getattr(document, "created_by", None),
        )
        db.commit()
    finally:
        db.close()

    try:
        _process_in_background(document_id)
    except Exception as exc:
        logger.warning(
            "Document processing attempt failed unexpectedly (%s)",
            type(exc).__name__,
        )
        _mark_unexpected_failure(make_session, document_id, attempt_number, exc)
        return None

    db = make_session()
    try:
        document = (
            db.query(Document)
            .filter(Document.id == document_id)
            .with_for_update()
            .first()
        )
        if document is None or document.status != "failed":
            return None
        if document.failure_class != "retryable":
            _attempt_audit(
                db,
                document,
                attempt=attempt_number,
                result="error",
                reason=document.failure_message,
                actor_id=getattr(document, "updated_by", None)
                or getattr(document, "created_by", None),
            )
            db.commit()
            return None
        if retry_index >= MAX_AUTOMATIC_RETRIES:
            document.failure_class = "retry_exhausted"
            document.next_retry_at = None
            document.failure_message = (
                "Document processing failed after three automatic retries. "
                "Use Retry to try again or upload the file again."
            )
            _attempt_audit(
                db,
                document,
                attempt=attempt_number,
                result="error",
                reason=document.failure_message,
                actor_id=getattr(document, "updated_by", None)
                or getattr(document, "created_by", None),
            )
            db.commit()
            return None

        delay = retry_delay_seconds(retry_index)
        document.retry_count = retry_index + 1
        document.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        _attempt_audit(
            db,
            document,
            attempt=retry_index + 1,
            result="warning",
            reason=document.failure_message,
            next_retry_at=document.next_retry_at,
            actor_id=getattr(document, "updated_by", None)
            or getattr(document, "created_by", None),
        )
        db.commit()
        return delay
    finally:
        db.close()


def process_document_with_retries(
    document_id: uuid.UUID,
    *,
    sleep: Callable[[float], None] = time.sleep,
    session_factory: Callable | None = None,
) -> None:
    """Non-Celery fallback using the same retry counts and delay schedule."""
    while True:
        delay = process_document_attempt(document_id, session_factory=session_factory)
        if delay is None:
            return
        sleep(delay)


def reprocess_documents_batch(
    db,
    actor_id: uuid.UUID,
    *,
    limit: int = 5,
    processor: Callable[[uuid.UUID], None] = process_document_with_retries,
) -> dict:
    """Explicitly reprocess one idempotent batch with at most five workers."""
    candidates = (
        db.query(Document)
        .filter(
            Document.bulk_reprocessed_at.is_(None),
            (Document.status == "needs_data_review")
            | ((Document.status == "failed") & (Document.failure_class == "retryable")),
        )
        .order_by(Document.created_at.asc(), Document.id.asc())
        .with_for_update(skip_locked=True)
        .limit(min(max(limit, 1), 5))
        .all()
    )
    selected_ids = [document.id for document in candidates]
    for document in candidates:
        document.bulk_reprocessed_at = datetime.now(UTC)
        document.status = "received"
        document.failure_class = None
        document.retry_count = 0
        document.next_retry_at = None
        document.failure_message = None
        db.add(
            AuditLog(
                action="document_bulk_reprocess_requested",
                entity_type="document",
                entity_id=document.id,
                result="success",
                message="Administrator requested document reprocessing",
                created_by=actor_id,
            )
        )
    db.commit()

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(processor, selected_ids))

    return {
        "scanned": len(selected_ids),
        "completed": len(selected_ids),
        "failed": sum(
            1 for document_id in selected_ids if _document_failed(document_id)
        ),
        "batch_size": 5,
        "remaining": _remaining_reprocessable(db),
    }


def _document_failed(document_id: uuid.UUID) -> bool:
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        return bool(document and document.status == "failed")
    finally:
        db.close()


def _remaining_reprocessable(db) -> int:
    return int(
        db.query(Document)
        .filter(
            Document.bulk_reprocessed_at.is_(None),
            (Document.status == "needs_data_review")
            | ((Document.status == "failed") & (Document.failure_class == "retryable")),
        )
        .count()
        or 0
    )
