import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class MatchRun(Base):
    __tablename__ = "match_runs"
    __table_args__ = (UniqueConstraint("invoice_id", "run_no", name="uq_match_run_no"),)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_no: Mapped[int] = mapped_column(Integer, nullable=False)
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchase_orders.id"),
    )
    result = mapped_column(
        ENUM(
            "match_ok",
            "match_issues",
            "no_match",
            name="match_result",
            schema="core",
            create_type=False,
        ),
        nullable=False,
    )
    exchange_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exchange_rates.id"),
    )
    rules_used = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    differences = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )

    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by = mapped_column(UUID(as_uuid=True), ForeignKey("auth.users.id"))
    updated_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id"),
    )
