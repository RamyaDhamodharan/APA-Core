import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import CHAR, ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_status", "status"),
        Index(
            "uq_documents_email_message_id_root",
            "email_message_id",
            unique=True,
            postgresql_where=text(
                "parent_document_id IS NULL AND email_message_id IS NOT NULL"
            ),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    file_hash: Mapped[str] = mapped_column(CHAR(64), unique=True, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    parent_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id")
    )
    status: Mapped[str] = mapped_column(
        ENUM(
            "received",
            "parsing",
            "parsed",
            "classified",
            "extracted",
            "needs_vendor_review",
            "needs_data_review",
            "processed",
            "ignored",
            "other",
            "incomplete",
            "duplicate_blocked",
            "failed",
            name="doc_status",
            schema="core",
            create_type=False,
        ),
        nullable=False,
        server_default=text("'received'"),
    )

    doc_type: Mapped[str | None] = mapped_column(
        ENUM(
            "po",
            "invoice",
            "goods_receipt",
            "other",
            name="doc_type",
            schema="core",
            create_type=False,
        ),
        nullable=True,
    )
    classify_confidence: Mapped[float | None] = mapped_column(nullable=True)
    classify_override: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size: Mapped[int | None] = mapped_column(nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text)
    extracted_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    email_sender: Mapped[str | None] = mapped_column(String(200))
    email_subject: Mapped[str | None] = mapped_column(String(500))
    email_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_message_id: Mapped[str | None] = mapped_column(String(300))
    email_uid: Mapped[int | None] = mapped_column(Integer)
    email_body_preview: Mapped[str | None] = mapped_column(Text)
    email_eml_key: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str | None] = mapped_column(String(40))
    source_flag: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    failure_class: Mapped[str | None] = mapped_column(String(24))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bulk_reprocessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
