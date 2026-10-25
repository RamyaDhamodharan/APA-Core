import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.vendor_schemas import VendorCreate


class PurchaseOrderLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    po_id: uuid.UUID
    item_code: str | None
    description: str | None
    uom: str | None
    qty: Decimal | None
    unit_price: Decimal | None
    line_amount: Decimal | None
    field_confidence: dict[str, Any] | None = None


class PurchaseOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    po_number: str
    version_no: int
    is_current: bool
    vendor_id: uuid.UUID | None
    vendor_name: str | None
    source_document_id: uuid.UUID | None
    po_date: date | None
    currency: str | None
    subtotal: Decimal | None
    tax_amount: Decimal | None
    total_amount: Decimal | None
    payment_terms: Any | None = None
    delivery_details: Any | None = None
    field_confidence: dict[str, Any] | None = None
    needs_verification: bool = False
    vendor_pending: bool = False
    verification_status: str = "verified"
    created_at: datetime
    updated_at: datetime
    lines: list[PurchaseOrderLineResponse]


class PurchaseOrderListResponse(BaseModel):
    items: list[PurchaseOrderResponse]
    total: int
    page: int
    page_size: int
    counts: dict[str, int] = Field(default_factory=dict)


class PurchaseOrderVendorResolutionRequest(BaseModel):
    vendor_id: uuid.UUID | None = None
    new_vendor: VendorCreate | None = None

    @model_validator(mode="after")
    def validate_vendor_choice(self):
        if (self.vendor_id is None) == (self.new_vendor is None):
            raise ValueError("Select an existing vendor or provide a new vendor")
        if self.new_vendor and not any(
            (
                self.new_vendor.tax_id,
                self.new_vendor.contact_email,
                self.new_vendor.contact_phone,
            )
        ):
            raise ValueError(
                "A new vendor requires a tax ID, contact email, or contact phone"
            )
        return self


class PurchaseOrderVendorReassignmentRequest(BaseModel):
    vendor_id: uuid.UUID
