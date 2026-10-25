import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.document_models import Document
from src.models.invoice_models import Invoice
from src.models.notification_models import Notification
from src.models.user_models import User
from src.schemas.notification_schemas import (
    ExceptionAlertItem,
    ExceptionAlertListResponse,
    NotificationItem,
    NotificationListResponse,
    NotificationStatus,
)
from src.services.email_draft_services import ensure_notification_draft, send_draft

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


@router.post("/{notification_id}/retry")
def retry_notification(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    notification = (
        db.query(Notification)
        .filter_by(id=notification_id)
        .with_for_update()
        .first()
    )
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.status == "sent":
        raise HTTPException(
            status_code=409, detail="Notification has already been sent"
        )
    draft = ensure_notification_draft(db, notification)
    sent = send_draft(db, draft, uuid.UUID(str(current_user["sub"])))
    notification.attempts = draft.attempts
    notification.sent_at = draft.sent_at
    notification.last_error = draft.last_error
    notification.status = "sent" if draft.status == "sent" else "failed"
    db.commit()
    return {
        "id": str(notification.id),
        "status": notification.status,
        "attempts": notification.attempts,
        "sent": sent,
    }


_EXCEPTION_MATCH_STATUSES = (
    "match_issues",
    "waiting_for_po",
    "needs_vendor_review",
)


def _notification_exception_type(
    invoice: Invoice | None,
) -> str:
    if invoice is None:
        return "notification"
    if invoice.match_status in _EXCEPTION_MATCH_STATUSES:
        return invoice.match_status
    if invoice.final_status is None:
        return "pending_review"
    return "notification"


@router.get(
    "",
    response_model=NotificationListResponse,
    dependencies=[Depends(get_current_user)],
)
def list_notifications(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: NotificationStatus | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> NotificationListResponse:
    try:
        user_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail="Authenticated user ID is invalid",
        ) from exc

    recipient_email = (
        db.query(User.email)
        .filter(
            User.id == user_id,
            User.is_active.is_(True),
        )
        .scalar()
    )
    if recipient_email is None:
        raise HTTPException(
            status_code=401,
            detail="Authenticated user was not found",
        )

    filters = [Notification.recipient_email == recipient_email]
    if status:
        filters.append(Notification.status == status)
    total = db.query(func.count(Notification.id)).filter(*filters).scalar()
    rows = (
        db.query(Notification, Invoice)
        .outerjoin(
            Document,
            Notification.document_id == Document.id,
        )
        .outerjoin(
            Invoice,
            Invoice.source_document_id == Document.id,
        )
        .filter(*filters)
        .order_by(Notification.created_at.desc(), Notification.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return NotificationListResponse(
        items=[
            NotificationItem(
                notification_id=notification.id,
                document_id=notification.document_id,
                invoice_id=invoice.id if invoice else None,
                invoice_no=invoice.invoice_no if invoice else None,
                exception_type=_notification_exception_type(invoice),
                status=notification.status,
                subject=notification.subject,
                body=notification.body,
                created_at=notification.created_at,
            )
            for notification, invoice in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/exception-alerts",
    response_model=ExceptionAlertListResponse,
    dependencies=[Depends(get_current_user)],
)
def list_exception_alerts(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: Session = Depends(get_db),
) -> ExceptionAlertListResponse:
    attention_filter = or_(
        Invoice.match_status.in_(_EXCEPTION_MATCH_STATUSES),
        Invoice.final_status.is_(None),
    )
    base_query = db.query(Invoice).filter(
        Invoice.is_current.is_(True),
        attention_filter,
    )
    total = base_query.with_entities(func.count(Invoice.id)).scalar()
    invoices = (
        base_query.order_by(Invoice.invoice_date.desc().nullslast(), Invoice.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return ExceptionAlertListResponse(
        items=[
            ExceptionAlertItem(
                invoice_id=invoice.id,
                invoice_no=invoice.invoice_no,
                exception_type=_notification_exception_type(invoice),
                status=invoice.match_status,
                final_status=invoice.final_status,
                created_at=invoice.created_at,
            )
            for invoice in invoices
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )
