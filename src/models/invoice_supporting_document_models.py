import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class InvoiceSupportingDocument(Base):
    __tablename__ = "invoice_supporting_documents"
    __table_args__ = (
        Index("ix_invoice_supporting_documents_invoice", "invoice_id"),
        CheckConstraint("size_bytes > 0", name="ck_invoice_supporting_size_positive"),
        Index(
            "uq_invoice_supporting_documents_active_sha256",
            "invoice_id",
            "sha256",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core.invoices.id"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(150), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(700), nullable=False)
    category: Mapped[str] = mapped_column(
        ENUM(
            "delivery_note",
            "contract",
            "approval_email",
            "tax_certificate",
            "other",
            name="invoice_supporting_document_category",
            schema="core",
            create_type=False,
        ),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    deleted_reason: Mapped[str | None] = mapped_column(Text)
