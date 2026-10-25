"""Canonical invoice-number keys used only for duplicate detection."""

import unicodedata
import uuid

from sqlalchemy.orm import Session

from src.models.invoice_models import Invoice


def normalize_invoice_no(value: object | None) -> str | None:
    """Normalize an invoice number while deliberately preserving leading zeroes."""
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value)).upper()
    normalized = "".join(
        char
        for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    return normalized or None


def find_duplicate_invoice(
    db: Session,
    vendor_id: uuid.UUID | None,
    invoice_no_normalized: str | None,
    *,
    exclude_invoice_id: uuid.UUID | None = None,
):
    if vendor_id is None or invoice_no_normalized is None:
        return None
    query = db.query(Invoice).filter(
        Invoice.vendor_id == vendor_id,
        Invoice.invoice_no_normalized == invoice_no_normalized,
        Invoice.is_current.is_(True),
    )
    if exclude_invoice_id is not None:
        query = query.filter(Invoice.id != exclude_invoice_id)
    return query.with_for_update().first()
