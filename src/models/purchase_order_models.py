import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.vendor_models import Base, Vendor


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("po_number", "version_no", name="uq_po_version"),
        Index(
            "uq_current_po_number",
            "po_number",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "uq_purchase_order_source_document",
            "source_document_id",
            unique=True,
            postgresql_where=text("source_document_id IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    po_number: Mapped[str] = mapped_column(String(60), nullable=False)
    version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id"),
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id"),
    )
    po_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payment_terms: Mapped[Any | None] = mapped_column(JSONB)
    delivery_details: Mapped[Any | None] = mapped_column(JSONB)
    field_confidence: Mapped[dict | None] = mapped_column(JSONB)
    needs_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
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
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    vendor: Mapped[Vendor | None] = relationship(lazy="selectin")

    @property
    def vendor_pending(self) -> bool:
        return self.vendor_id is None

    @property
    def verification_status(self) -> str:
        return (
            "needs_verification"
            if self.needs_verification or self.vendor_id is None
            else "verified"
        )

    @property
    def vendor_name(self) -> str | None:
        return self.vendor.name if self.vendor is not None else None


class PurchaseOrderLine(Base):
    __tablename__ = "po_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    po_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_code: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(String(300))
    uom: Mapped[str | None] = mapped_column(String(10))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    line_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    field_confidence: Mapped[dict | None] = mapped_column(JSONB)
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
    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
