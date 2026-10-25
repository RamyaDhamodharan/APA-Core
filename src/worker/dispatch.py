"""Choose between Celery and FastAPI BackgroundTasks for processing."""

import logging
import os
import uuid

from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)


def dispatch_document_processing(
    background_tasks: BackgroundTasks,
    document_id: uuid.UUID,
    fallback,
) -> str:
    """Queue processing on Celery or use the same bounded retry fallback."""
    if os.getenv("USE_CELERY", "false").lower() == "true":
        try:
            from src.worker.tasks import process_document

            process_document.delay(str(document_id))
            return "celery"
        except Exception as exc:
            logger.error(
                "Celery dispatch failed for %s (%s); using in-process fallback",
                document_id,
                type(exc).__name__,
            )

    background_tasks.add_task(fallback, document_id)
    return "background"


def dispatch_raw_email_recovery(
    background_tasks: BackgroundTasks,
    document_id: uuid.UUID,
) -> str:
    """Recover persisted attachments instead of parsing an email container."""
    if os.getenv("USE_CELERY", "false").lower() == "true":
        try:
            from src.worker.tasks import recover_raw_email

            recover_raw_email.delay(str(document_id))
            return "celery"
        except Exception as exc:
            logger.error(
                "Celery raw email recovery dispatch failed for %s (%s)",
                document_id,
                type(exc).__name__,
            )

    from src.services.document_recovery_services import recover_raw_email_document

    background_tasks.add_task(recover_raw_email_document, document_id)
    return "background"


def dispatch_notification(
    background_tasks: BackgroundTasks | None, notification_id: uuid.UUID
) -> str:
    """Dispatch queued email, falling back to the request background task."""
    if os.getenv("USE_CELERY", "false").lower() == "true":
        try:
            from src.worker.tasks import send_notification

            send_notification.delay(str(notification_id))
            return "celery"
        except Exception as exc:
            logger.error(
                "Celery notification dispatch failed for %s (%s)",
                notification_id,
                type(exc).__name__,
            )
    if background_tasks is not None:
        background_tasks.add_task(_send_notification_in_process, notification_id)
        return "background"
    _send_notification_in_process(notification_id)
    return "inline"


def _send_notification_in_process(notification_id: uuid.UUID) -> None:
    from src.db.session_db import SessionLocal
    from src.models.notification_models import Notification
    from src.services.email_draft_services import ensure_notification_draft, send_draft

    db = SessionLocal()
    try:
        notification = (
            db.query(Notification)
            .filter_by(id=notification_id)
            .with_for_update()
            .first()
        )
        if notification is not None and notification.status == "queued":
            draft = ensure_notification_draft(db, notification)
            auto_send_subjects = {
                "No bill attachment found",
                "Invoice needs attention",
                "Invoice could not be matched",
            }
            auto_send_internal = (notification.subject or "").startswith(
                "AP internal reminder:"
            )
            if (
                notification.subject not in auto_send_subjects
                and not auto_send_internal
            ):
                notification.status = "failed"
                notification.last_error = (
                    "Saved as an email draft for human review before sending"
                )
                db.commit()
                return
            send_draft(db, draft, notification.created_by)
            notification.attempts = draft.attempts
            notification.sent_at = draft.sent_at
            notification.last_error = draft.last_error
            notification.status = "sent" if draft.status == "sent" else "failed"
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Notification dispatch failed for %s (%s)",
            notification_id,
            type(exc).__name__,
        )
    finally:
        db.close()


def dispatch_queued_notifications(db, document_id, background_tasks=None) -> int:
    from src.models.notification_models import Notification

    try:
        rows = (
            db.query(Notification)
            .filter(
                Notification.document_id == document_id,
                Notification.status == "queued",
            )
            .all()
        )
    except Exception as exc:
        logger.error(
            "Could not load queued notifications for %s (%s)",
            document_id,
            type(exc).__name__,
        )
        return 0
    for row in rows:
        dispatch_notification(background_tasks, row.id)
    return len(rows)
