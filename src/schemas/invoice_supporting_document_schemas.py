from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

SupportingDocumentCategory = Literal[
    "delivery_note", "contract", "approval_email", "tax_certificate", "other"
]


class SupportingDocumentItem(BaseModel):
    id: UUID
    file_name: str
    content_type: str
    size_bytes: int
    category: SupportingDocumentCategory
    description: str | None
    uploaded_by: UUID
    uploaded_by_name: str | None
    uploaded_at: datetime
    deleted_at: datetime | None = None
    deleted_by: UUID | None = None
    deleted_reason: str | None = None
