from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

InvoiceMatchStatus = Literal[
    "pending",
    "needs_vendor_review",
    "waiting_for_po",
    "needs_po_confirmation",
    "match_issues",
    "no_match",
]
InvoiceStatus = Literal[
    "processing", "needs_review", "ready_to_pay", "on_hold", "rejected", "paid"
]
InvoiceFinalStatus = Literal["approved", "rejected", "on_hold", "paid"]
InvoiceWorkflowStatus = Literal[
    "received",
    "reading",
    "verifying",
    "matching",
    "match_approved",
    "match_issues",
    "no_match",
    "approved",
    "rejected",
    "on_hold",
    "paid",
    "overdue",
    "resubmitted",
    "failed",
]


class InvoiceReviewRequest(BaseModel):
    decision: InvoiceFinalStatus
    comment: str | None = None
    reason: str | None = None
    override_reason: str | None = None


class InvoiceReviewResponse(BaseModel):
    invoice_id: UUID
    final_status: InvoiceFinalStatus
    final_by: UUID
    final_at: datetime


class InvoiceDashboardItem(BaseModel):
    invoice_id: UUID
    source_document_id: UUID | None = None
    received_at: datetime | None = None
    processing_status: str | None = None
    source: str | None = None
    email_sender: str | None = None
    email_subject: str | None = None
    invoice_no: str | None
    vendor_name: str | None
    invoice_date: date | None
    due_date: date | None
    total_amount: Decimal | None
    currency: str | None
    match_status: InvoiceMatchStatus
    final_status: InvoiceFinalStatus | None
    workflow_status: InvoiceWorkflowStatus = "received"
    gl_code: str | None = None
    confidence: float | None = None
    # Display-only overall level derived from the minimum invoice field
    # confidence (unavailable values are treated as low).
    confidence_level: str = "low"
    overall_confidence_percent: int = 0
    can_resubmit: bool = False
    status: InvoiceStatus = "processing"
    discrepancy_count: int = 0
    active_discrepancy_count: int = 0


class InvoiceDashboardResponse(BaseModel):
    items: list[InvoiceDashboardItem]
    total: int
    page: int
    page_size: int
