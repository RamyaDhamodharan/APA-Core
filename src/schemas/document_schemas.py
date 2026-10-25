import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    status: str
    doc_type: str | None
    failure_message: str | None
    failure_class: str | None = None
    retry_count: int = 0
    next_retry_at: datetime | None = None
    last_attempt_at: datetime | None = None
    source: str | None = None
    parent_document_id: uuid.UUID | None = None
    email_sender: str | None = None
    email_subject: str | None = None
    email_received_at: datetime | None = None
    email_message_id: str | None = None
    linked_invoice_id: uuid.UUID | None = None
    linked_invoice_status: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    raw_text: str | None
    extracted_json: dict[str, Any] | None
    linked_po_id: uuid.UUID | None = None
    linked_gr_id: uuid.UUID | None = None


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int
    page: int
    page_size: int


class DocumentAuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: str
    result: str
    message: str | None
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    step_name: str | None = None
    duration_ms: int | None = None
    input_ref: str | None = None
    output_summary: dict[str, Any] | None = None
    reason: str | None = None
    created_by: uuid.UUID | None
    created_at: datetime


class DocumentReviewDetail(DocumentDetail):
    audit_history: list[DocumentAuditEntry]
