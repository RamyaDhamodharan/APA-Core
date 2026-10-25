import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class EmailPollState(Base):
    __tablename__ = "email_poll_state"
    __table_args__ = {"schema": "core"}

    mailbox: Mapped[str] = mapped_column(String(300), primary_key=True)
    folder: Mapped[str] = mapped_column(String(300), primary_key=True)
    uidvalidity: Mapped[int] = mapped_column(Integer, nullable=False)
    last_uid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_email_messages_message_id"),
        Index("ix_email_messages_mailbox_folder_uid", "mailbox", "folder", "uid"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    uid: Mapped[int] = mapped_column(Integer, nullable=False)
    uidvalidity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    mailbox: Mapped[str] = mapped_column(String(300), nullable=False)
    folder: Mapped[str] = mapped_column(String(300), nullable=False, default="INBOX")
    sender: Mapped[str | None] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body_preview: Mapped[str | None] = mapped_column(Text)
    raw_object_key: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="received",
        server_default=text("'received'"),
    )
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    filter_reason: Mapped[str | None] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
