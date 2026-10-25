import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_due_date", "due_date"),
        Index("ix_invoices_owner", "owner_user_id"),
        Index("ix_invoices_po_id", "po_id"),
        Index(
            "ix_invoices_status_date",
            "match_status",
            "final_status",
            "invoice_date",
        ),
        UniqueConstraint(
            "vendor_id",
            "invoice_no",
            "version_no",
            name="uq_invoice_version",
        ),
        Index(
            "uq_current_vendor_invoice_normalized",
            "vendor_id",
            "invoice_no_normalized",
            unique=True,
            postgresql_where=text(
                "is_current AND vendor_id IS NOT NULL "
                "AND invoice_no_normalized IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    status: Mapped[str] = mapped_column(
        ENUM(
            "processing",
            "needs_review",
            "ready_to_pay",
            "on_hold",
            "rejected",
            name="invoice_status",
            schema="core",
            create_type=False,
        ),
        nullable=False,
        server_default=text("'processing'"),
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id"),
    )
    resubmitted_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    invoice_no: Mapped[str | None] = mapped_column(String(60))
    invoice_no_normalized: Mapped[str | None] = mapped_column(String(180))
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id"),
    )
    vendor_name_raw: Mapped[str | None] = mapped_column(String(200))
    vendor_tax_id_raw: Mapped[str | None] = mapped_column(String(30))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    field_confidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    gl_code: Mapped[str | None] = mapped_column(String(60))
    po_number_raw: Mapped[str | None] = mapped_column(String(60))
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchase_orders.id"),
    )
    match_status: Mapped[str] = mapped_column(
        ENUM(
            "pending",
            "needs_vendor_review",
            "waiting_for_po",
            "needs_po_confirmation",
            "match_issues",
            "no_match",
            name="match_status",
            schema="core",
            create_type=False,
        ),
        nullable=False,
        server_default=text("'pending'"),
    )
    final_status: Mapped[str | None] = mapped_column(
        ENUM(
            "approved",
            "rejected",
            "on_hold",
            name="review_decision",
            schema="core",
            create_type=False,
        ),
    )
    workflow_status: Mapped[str] = mapped_column(
        ENUM(
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
            name="invoice_workflow_status",
            schema="core",
            create_type=False,
        ),
        nullable=False,
        server_default=text("'received'"),
    )
    final_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("match_runs.id")
    )
    final_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    final_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_method: Mapped[str | None] = mapped_column(String(60))
    payment_reference: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_code: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(String(300))
    uom: Mapped[str | None] = mapped_column(String(10))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    line_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    field_confidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    po_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("po_lines.id")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def _set_invoice_number_key(_mapper, _connection, target: Invoice) -> None:
    """Keep the duplicate-detection key synchronized for every ORM write path."""
    from src.services.invoice_number_services import normalize_invoice_no

    target.invoice_no_normalized = normalize_invoice_no(target.invoice_no)


event.listen(Invoice, "before_insert", _set_invoice_number_key)
event.listen(Invoice, "before_update", _set_invoice_number_key)
