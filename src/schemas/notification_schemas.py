from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

NotificationStatus = Literal["queued", "sent", "failed"]
InvoiceExceptionType = Literal[
    "match_issues",
    "waiting_for_po",
    "needs_vendor_review",
    "pending_review",
]


class NotificationItem(BaseModel):
    notification_id: UUID
    document_id: UUID
    invoice_id: UUID | None
    invoice_no: str | None
    exception_type: InvoiceExceptionType | Literal["notification"]
    status: NotificationStatus
    subject: str | None
    body: str | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    total: int
    page: int
    page_size: int


class ExceptionAlertItem(BaseModel):
    invoice_id: UUID
    invoice_no: str | None
    exception_type: InvoiceExceptionType
    status: str
    final_status: str | None
    created_at: datetime


class ExceptionAlertListResponse(BaseModel):
    items: list[ExceptionAlertItem]
    total: int
    page: int
    page_size: int
