import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.services.vendor_resolution_services import normalize_vendor_name


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    tax_id: str | None = None
    default_tax_rate: Decimal | None = None
    is_active: bool
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


class VendorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=30)
    default_tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=40)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Vendor name must not be blank")
        return value

    @field_validator("tax_id", "contact_name", "contact_phone", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class VendorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=30)
    default_tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if value is None:
            raise ValueError("Vendor name must not be null")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Vendor name must not be blank")
        return value

    @field_validator("tax_id", "contact_name", "contact_phone", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("is_active", mode="before")
    @classmethod
    def require_active_value_when_provided(cls, value: object) -> object:
        if value is None:
            raise ValueError("is_active must not be null")
        return value


class VendorAliasCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=200)

    @field_validator("alias", mode="before")
    @classmethod
    def normalize_alias(cls, value: object) -> str:
        normalized = normalize_vendor_name(value)
        if not normalized:
            raise ValueError("Vendor alias must not be blank")
        return normalized


class VendorAliasUpdate(BaseModel):
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    vendor_id: uuid.UUID | None = None
    is_active: bool | None = None

    @field_validator("alias", mode="before")
    @classmethod
    def normalize_alias(cls, value: object) -> str:
        if value is None:
            raise ValueError("Vendor alias must not be null")
        normalized = normalize_vendor_name(value)
        if not normalized:
            raise ValueError("Vendor alias must not be blank")
        return normalized

    @field_validator("vendor_id", "is_active", mode="before")
    @classmethod
    def reject_null_updates(cls, value: object) -> object:
        if value is None:
            raise ValueError("Updated fields must not be null")
        return value


class VendorAliasResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_id: uuid.UUID
    alias_norm: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
