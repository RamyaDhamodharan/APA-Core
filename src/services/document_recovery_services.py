"""Recover documents whose background task stopped unexpectedly."""

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

from sqlalchemy import and_, or_

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage

logger = logging.getLogger(__name__)


def recover_raw_email_document(document_id: uuid.UUID) -> None:
    """Recover attachments for one raw email document after an explicit retry."""
    from src.db.session_db import SessionLocal
    from src.services.email_polling_services import _sanitize_exception_message

    db = SessionLocal()
    try:
        document = (
            db.query(Document).filter_by(id=document_id).with_for_update().first()
        )
        if document is None:
            return
        try:
            outcome = _recover_raw_email(db, document)
        except Exception as exc:
            detail = _sanitize_exception_message(str(exc))
            logger.error(
                "Raw email recovery failed for %s (%s): %s",
                document_id,
                type(exc).__name__,
                detail or "No exception message",
            )
            outcome = "Raw email recovery failed"

        email_message = (
            db.query(EmailMessage)
            .filter(EmailMessage.message_id == document.email_message_id)
            .first()
            if document.email_message_id
            else None
        )
        if outcome == "attachments recovered":
            document.status = "processed"
            document.failure_message = None
            if email_message:
                email_message.status = "processed"
                email_message.failure_reason = None
                email_message.filter_reason = None
                email_message.next_retry_at = None
        elif outcome in {
            "No attachment found",
            "Only an inline image found, send a PDF",
            "Unsupported attachment type",
        }:
            document.status = "ignored"
            document.failure_message = outcome
            if email_message:
                email_message.status = "needs_attention"
                email_message.failure_reason = outcome
                email_message.filter_reason = outcome
                email_message.next_retry_at = None
        else:
            document.status = "failed"
            document.failure_message = (
                "Email attachments could not be recovered; retry or upload "
                "them directly."
            )
            if email_message:
                email_message.status = "failed_final"
                email_message.failure_reason = document.failure_message
                email_message.next_retry_at = None
        db.add(
            AuditLog(
                action="document_raw_email_recovered",
                entity_type="document",
                entity_id=document.id,
                result="success" if outcome == "attachments recovered" else "error",
                message="Raw email attachment recovery completed",
                reason=outcome,
                new_value={"outcome": outcome},
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Raw email retry failed for %s (%s): %s",
            document_id,
            type(exc).__name__,
            _safe_recovery_error(exc),
        )
    finally:
        db.close()


def _safe_recovery_error(error: Exception) -> str:
    from src.services.email_polling_services import _sanitize_exception_message

    return _sanitize_exception_message(str(error)) or "No exception message"


def _document_attempts(db, document_id) -> int:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "document",
            AuditLog.entity_id == document_id,
            AuditLog.action == "document_stuck_requeued",
        )
        .count()
    )


def _recover_raw_email(db, document: Document) -> str:
    """Extract supported attachments from a persisted raw EML root, idempotently."""
    from src.services.document_ingest_services import ingest_file
    from src.services.email_polling_services import (
        _attachment_format,
        _referenced_content_ids,
        _safe_filename,
        _skip_image_part,
    )
    from src.storage.minio_storage import download_file

    if not document.storage_path:
        raise RuntimeError("Raw email is missing its stored message")
    raw = download_file(document.storage_path)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    referenced_cids = _referenced_content_ids(message)
    children = (
        db.query(Document).filter(Document.parent_document_id == document.id).all()
    )
    existing_hashes = {child.file_hash for child in children}
    created_or_found = 0
    attachment_candidate_seen = False
    inline_image_seen = False
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not (filename or disposition in {"attachment", "inline"}):
            continue
        attachment_candidate_seen = True
        payload = part.get_payload(decode=True) or b""
        content_type = part.get_content_type().casefold()
        if _skip_image_part(part, payload, referenced_cids):
            continue
        detected = _attachment_format(part, payload)
        if detected is None:
            inline_image_seen = inline_image_seen or (
                content_type.startswith("image/")
                and disposition == "inline"
                and len(payload) >= 20 * 1024
            )
            continue
        extension, _ = detected
        safe_name = _safe_filename(filename or f"attachment{extension}", extension)
        digest = hashlib.sha256(payload).hexdigest()
        existing = db.query(Document).filter_by(file_hash=digest).first()
        if existing or digest in existing_hashes:
            created_or_found += 1
            continue
        result = ingest_file(
            db,
            safe_name,
            payload,
            content_type,
            None,
            "email",
            parent_document_id=document.id,
            email_meta={
                "email_sender": document.email_sender,
                "email_subject": document.email_subject,
                "email_received_at": document.email_received_at,
                "email_message_id": document.email_message_id,
                "email_uid": document.email_uid,
                "email_body_preview": document.email_body_preview,
                "email_eml_key": document.email_eml_key or document.storage_path,
            },
            document_id=uuid.uuid4(),
        )
        if result.status in {"received", "duplicate_blocked"}:
            created_or_found += 1
        else:
            raise RuntimeError(result.message or "Attachment could not be saved")

    if not created_or_found:
        if inline_image_seen:
            return "Only an inline image found, send a PDF"
        if attachment_candidate_seen:
            return "Unsupported attachment type"
        return "No attachment found"
    return "attachments recovered"


def sweep_stuck_documents(db, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    timeout_minutes = max(1, int(os.getenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15")))
    from src.services.document_retry_services import MAX_AUTOMATIC_RETRIES

    max_attempts = MAX_AUTOMATIC_RETRIES
    cutoff = current - timedelta(minutes=timeout_minutes)
    stuck = (
        db.query(Document)
        .filter(
            or_(
                and_(
                    Document.status.in_(
                        ["received", "parsing", "parsed", "classified"]
                    ),
                    Document.updated_at < cutoff,
                ),
                and_(
                    Document.status == "failed",
                    Document.failure_class == "retryable",
                    Document.next_retry_at <= current,
                ),
            )
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    outcomes = {
        "scanned": len(stuck),
        "requeued": 0,
        "recovered": 0,
        "failed": 0,
        "needs_attention": 0,
    }
    for document in stuck:
        if (
            document.source == "email"
            and document.parent_document_id is None
            and (
                document.original_filename.casefold().endswith(".eml")
                or (document.mime or "").casefold() == "message/rfc822"
            )
        ):
            try:
                outcome = _recover_raw_email(db, document)
            except Exception as exc:
                from src.services.email_polling_services import (
                    _sanitize_exception_message,
                )

                detail = _sanitize_exception_message(str(exc))
                outcome = (
                    f"Raw email recovery failed: {detail}"
                    if detail
                    else f"Raw email recovery failed: {type(exc).__name__}"
                )[:200]
            if outcome in {
                "No attachment found",
                "Only an inline image found, send a PDF",
                "Unsupported attachment type",
            }:
                document.status = "ignored"
                document.failure_message = outcome
                outcomes["needs_attention"] += 1
                message = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.message_id == document.email_message_id)
                    .first()
                )
                if message:
                    message.status = "needs_attention"
                    message.failure_reason = outcome
                    message.filter_reason = outcome
                    message.next_retry_at = None
            elif outcome.startswith("Raw email recovery failed:"):
                document.status = "failed"
                document.failure_message = outcome
                outcomes["failed"] += 1
                message = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.message_id == document.email_message_id)
                    .first()
                )
                if message:
                    message.status = "failed_final"
                    message.failure_reason = outcome
                    message.next_retry_at = None
            else:
                document.status = "processed"
                document.failure_message = None
                outcomes["recovered"] += 1
                message = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.message_id == document.email_message_id)
                    .first()
                )
                if message:
                    message.status = "processed"
                    message.failure_reason = None
                    message.filter_reason = None
            db.add(
                AuditLog(
                    action="document_raw_email_recovered",
                    entity_type="document",
                    entity_id=document.id,
                    result="success"
                    if not outcome.startswith("Raw email")
                    else "error",
                    message="Stuck raw email recovery completed",
                    reason=outcome,
                    new_value={"outcome": outcome},
                )
            )
            logger.info(
                "stuck_document_recovery %s",
                json.dumps({"document_id": str(document.id), "outcome": outcome}),
            )
            continue

        due_retry = document.status == "failed"
        attempts = max(int(getattr(document, "retry_count", 0) or 0), 0)
        if due_retry:
            document.status = "received"
            document.next_retry_at = None
            delay = 0
            outcomes["requeued"] += 1
            outcome = "retry_recovered"
        elif attempts >= max_attempts:
            reason = (
                "Document processing stopped unexpectedly after three automatic "
                "retries. Use Retry to try again."
            )
            document.status = "failed"
            document.failure_class = "retry_exhausted"
            document.next_retry_at = None
            document.failure_message = reason
            outcomes["failed"] += 1
            outcome = "failed_max_attempts"
        else:
            from src.services.document_retry_services import retry_delay_seconds

            delay = retry_delay_seconds(attempts)
            document.retry_count = attempts + 1
            document.failure_class = "retryable"
            document.next_retry_at = current + timedelta(seconds=delay)
            document.failure_message = (
                "Document processing stopped unexpectedly; an automatic retry "
                "has been scheduled."
            )
            document.status = "failed"
            outcomes["requeued"] += 1
            outcome = "requeued"
        db.add(
            AuditLog(
                action="document_stuck_requeued"
                if outcome in {"requeued", "retry_recovered"}
                else "document_stuck_failed",
                entity_type="document",
                entity_id=document.id,
                result="success"
                if outcome in {"requeued", "retry_recovered"}
                else "error",
                message="Stuck document recovery processed",
                reason=document.failure_message,
                new_value={
                    "outcome": outcome,
                    "attempt": attempts + 1,
                    "next_retry_at": (
                        document.next_retry_at.isoformat()
                        if document.next_retry_at
                        else None
                    ),
                },
            )
        )
        db.commit()
        if outcome in {"requeued", "retry_recovered"}:
            try:
                from src.worker.tasks import process_document

                process_document.apply_async(
                    args=[str(document.id)],
                    queue="documents",
                    countdown=delay,
                )
            except Exception as exc:
                reason = f"Could not queue document recovery: {type(exc).__name__}"
                document.status = "failed"
                document.failure_class = "retryable"
                document.failure_message = reason
                document.next_retry_at = current + timedelta(seconds=60)
                outcomes["requeued"] -= 1
                outcomes["failed"] += 1
                outcome = "queue_failed"
                db.add(
                    AuditLog(
                        action="document_stuck_failed",
                        entity_type="document",
                        entity_id=document.id,
                        result="error",
                        message="Stuck document recovery could not be queued",
                        reason=reason,
                        new_value={"outcome": outcome, "attempt": attempts + 1},
                    )
                )
                db.commit()
        logger.info(
            "stuck_document_recovery %s",
            json.dumps(
                {
                    "document_id": str(document.id),
                    "outcome": outcome,
                    "attempt": attempts + 1,
                    "reason": document.failure_message,
                },
                sort_keys=True,
            ),
        )

    failed_email_documents = (
        db.query(Document)
        .filter(Document.source == "email", Document.status == "failed")
        .all()
    )
    for document in failed_email_documents:
        if (
            document.source != "email"
            or document.status != "failed"
            or not document.email_message_id
        ):
            continue
        message = (
            db.query(EmailMessage)
            .filter(EmailMessage.message_id == document.email_message_id)
            .first()
        )
        if message and message.status not in {
            "failed",
            "failed_final",
            "needs_attention",
        }:
            message.status = "failed_final"
            message.failure_reason = (
                document.failure_message or "Email attachment processing failed"
            )[:200]
            message.next_retry_at = None
    db.commit()
    logger.info("stuck_document_recovery_run %s", json.dumps(outcomes, sort_keys=True))
    return len(stuck)
