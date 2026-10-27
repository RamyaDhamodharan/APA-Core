"""Admin endpoints for on-demand email polling and poll status."""

import os
from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user, require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage, EmailPollState
from src.models.invoice_models import Invoice
from src.services.docling_model_services import docling_models_available
from src.services.email_polling_services import poll_email_with_lock
from src.worker.dispatch import dispatch_document_processing

router = APIRouter(prefix="/api/email-polling", tags=["email-polling"])
email_router = APIRouter(prefix="/api/email", tags=["email"])


def _require_finance_staff(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in {"admin", "associate"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    return current_user


def _retry_email_uid_in_background(uid: int) -> None:
    from src.db.session_db import SessionLocal
    from src.services.email_polling_services import poll_email_with_lock

    db = SessionLocal()
    try:
        poll_email_with_lock(db, force_uids={uid})
    finally:
        db.close()


@email_router.post("/backfill")
def backfill_email_invoices(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("admin")),
):
    """Backfill extracted invoices and safely queue exhausted email retries."""
    from src.services.email_invoice_backfill_services import (
        backfill_extracted_invoices,
    )

    actor_id = UUID(str(current_user["sub"])) if current_user.get("sub") else None
    summary = backfill_extracted_invoices(db, actor_id)
    email_ids = summary.pop("email_retry_ids", [])
    queued_documents = 0
    for email_id in email_ids:
        row = db.query(EmailMessage).filter_by(id=UUID(email_id)).first()
        if row is None:
            continue
        documents = (
            db.query(Document)
            .filter(
                Document.email_message_id == row.message_id,
                Document.status == "failed",
            )
            .all()
        )
        for document in documents:
            document.status = "received"
            document.failure_message = None
            from src.api.rest.routers.document_routers import _process_in_background

            dispatch_document_processing(
                background_tasks, document.id, _process_in_background
            )
            queued_documents += 1
        if os.getenv("USE_CELERY", "false").casefold() == "true":
            try:
                from src.worker.tasks import retry_email_message as retry_task

                retry_task.delay(str(row.id))
            except Exception:
                pass
        else:
            background_tasks.add_task(_retry_email_uid_in_background, row.uid)
    db.commit()
    summary["documents_queued"] = queued_documents
    return summary


@router.post("/recover-stuck")
def recover_stuck_documents(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("admin")),
):
    """Run the audited stuck-document recovery/backfill on demand."""
    from src.services.document_recovery_services import sweep_stuck_documents

    swept = sweep_stuck_documents(db)
    db.add(
        AuditLog(
            action="document_recovery_backfill_requested",
            entity_type="system",
            result="success",
            message="Administrator ran stuck-document recovery",
            output_summary={"documents_swept": swept},
            created_by=UUID(str(current_user["sub"]))
            if current_user.get("sub")
            else None,
        )
    )
    db.commit()
    return {"documents_swept": swept}


@email_router.post("/poll")
@router.post("/run")
def run_email_poll(
    db: Session = Depends(get_db),
    current_user: dict = Depends(_require_finance_staff),
):
    db.add(
        AuditLog(
            action="email_poll_requested",
            entity_type="email",
            result="success",
            message="Finance staff requested an email poll",
            created_by=UUID(str(current_user["sub"]))
            if current_user.get("sub")
            else None,
        )
    )
    db.commit()
    return poll_email_with_lock(db)


@email_router.get("/status")
@router.get("/status")
def email_poll_status(
    db: Session = Depends(get_db),
    _user: dict = Depends(_require_finance_staff),
):
    mailbox = os.getenv("IMAP_USER", "").strip()
    folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    state = db.query(EmailPollState).filter_by(mailbox=mailbox, folder=folder).first()
    interval = max(1, int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300")))
    last_run = state.last_run_at if state else None
    messages = db.query(EmailMessage).all()
    failures = sum(row.status in {"failed", "failed_final"} for row in messages)
    processed = sum(row.status == "processed" for row in messages)
    needs_attention = sum(row.status == "needs_attention" for row in messages)
    skipped = sum(
        row.status == "ignored" or (bool(row.filter_reason) and row.status != "processed")
        for row in messages
    )
    skipped_by_reason = Counter(
        row.filter_reason for row in messages if row.filter_reason
    )
    latest_error = next(
        (
            row.failure_reason
            for row in sorted(
                messages,
                key=lambda item: (
                    item.created_at.timestamp()
                    if isinstance(item.created_at, datetime)
                    else 0
                ),
                reverse=True,
            )
            if row.status in {"failed", "failed_final"} and row.failure_reason
        ),
        None,
    )
    last_result = state.last_result if state and state.last_result else {}
    last_error = last_result.get("message") or latest_error
    return {
        "enabled": (
            os.getenv("EMAIL_POLL_ENABLED", "false").lower() == "true"
            and os.getenv("EMAIL_PROVIDER", "imap").lower() == "imap"
        ),
        "last_uid": state.last_uid if state else None,
        "uidvalidity": state.uidvalidity if state else None,
        "last_run_at": last_run.isoformat() if last_run else None,
        "next_poll_at": (last_run + timedelta(seconds=interval)).isoformat()
        if last_run
        else None,
        "last_error": last_error,
        "docling_models_available": docling_models_available(),
        "counts": {
            "total": len(messages),
            "processed": processed,
            "failed": failures,
            "needs_attention": needs_attention,
            "skipped": skipped,
            "skipped_by_reason": dict(skipped_by_reason),
        },
        "last_result": state.last_result
        if state and state.last_result
        else {
            "emails": 0,
            "attachments": 0,
            "duplicates": 0,
            "skipped_known": 0,
            "filtered": 0,
            "failed": 0,
            "filtered_by_reason": {},
        },
        "filtered_by_reason": (
            state.last_result.get("filtered_by_reason", {})
            if state and state.last_result
            else {}
        ),
    }


@email_router.get("/messages")
@router.get("/messages")
def email_poll_messages(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, max_length=40),
    reason: str | None = Query(None, max_length=200),
    db: Session = Depends(get_db),
    _user: dict = Depends(_require_finance_staff),
):
    query = db.query(EmailMessage)
    if status == "skipped":
        query = query.filter(EmailMessage.status == "ignored")
    elif status == "failed":
        query = query.filter(EmailMessage.status.in_(["failed", "failed_final"]))
    elif isinstance(status, str) and status:
        query = query.filter(EmailMessage.status == status)
    if not isinstance(reason, str):
        reason = None
    if reason and reason.strip():
        reason_pattern = f"%{reason.strip()}%"
        query = query.filter(
            (EmailMessage.filter_reason.ilike(reason_pattern))
            | (EmailMessage.failure_reason.ilike(reason_pattern))
        )
    total = query.count()
    rows = (
        query
        .order_by(EmailMessage.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    linked_documents = {
        row.email_message_id: row
        for row in db.query(Document)
        .filter(Document.email_message_id.in_([item.message_id for item in rows]))
        .order_by(Document.created_at.desc())
        .all()
        if row.email_message_id
    } if rows else {}
    linked_invoice_by_document = {
        invoice.source_document_id: invoice
        for invoice in db.query(Invoice)
        .filter(
            Invoice.source_document_id.in_(
                [document.id for document in linked_documents.values()]
            )
        )
        .all()
    } if linked_documents else {}
    return {
        "items": [
        {
            "id": str(row.id),
            "message_id": row.message_id,
            "uid": row.uid,
            "sender": row.sender,
            "subject": row.subject,
            "status": row.status,
            "filter_reason": row.filter_reason,
            "failure_reason": row.failure_reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "received_at": row.received_at.isoformat() if row.received_at else None,
            "retryable": row.status in {"failed", "failed_final"},
            "document_id": (
                str(linked_documents[row.message_id].id)
                if row.message_id in linked_documents
                else None
            ),
            "linked_invoice_id": (
                str(linked_invoice_by_document[linked_documents[row.message_id].id].id)
                if row.message_id in linked_documents
                and linked_documents[row.message_id].id in linked_invoice_by_document
                else None
            ),
            "linked_invoice_status": (
                linked_invoice_by_document[linked_documents[row.message_id].id].status
                if row.message_id in linked_documents
                and linked_documents[row.message_id].id in linked_invoice_by_document
                else None
            ),
        }
        for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@email_router.post("/messages/{message_id}/retry", status_code=202)
@router.post("/messages/{message_id}/retry", status_code=202)
def retry_email_message(
    message_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(_require_finance_staff),
):
    row = (
        db.query(EmailMessage)
        .filter(EmailMessage.id == message_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Email message not found")
    if row.status not in {"failed", "failed_final"}:
        raise HTTPException(status_code=409, detail="Only failed emails can be retried")
    row.status = "failed"
    row.attempts = 0
    row.next_retry_at = None
    row.failure_reason = None
    db.add(
        AuditLog(
            action="email_retry_requested",
            entity_type="email",
            entity_id=row.id,
            result="success",
            message="Finance staff requested email reprocessing",
            created_by=UUID(str(current_user["sub"]))
            if current_user.get("sub")
            else None,
        )
    )
    db.commit()
    if os.getenv("USE_CELERY", "false").lower() == "true":
        try:
            from src.worker.tasks import retry_email_message as retry_task

            retry_task.delay(str(row.id))
        except Exception:
            # Keep the row retryable if Celery is temporarily unavailable.
            pass
    else:
        background_tasks.add_task(_retry_email_uid_in_background, row.uid)
    documents = (
        db.query(Document)
        .filter(
            Document.email_message_id == row.message_id,
            Document.status.in_(["failed", "needs_data_review"]),
        )
        .all()
    )
    for document in documents:
        document.status = "received"
        document.failure_message = None
        from src.api.rest.routers.document_routers import _process_in_background

        dispatch_document_processing(
            background_tasks, document.id, _process_in_background
        )
    db.commit()
    return {"id": str(row.id), "status": row.status, "documents_queued": len(documents)}
