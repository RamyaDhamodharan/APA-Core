import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Index

from src.models.invoice_models import Invoice, _set_invoice_number_key
from src.services.invoice_number_services import (
    find_duplicate_invoice,
    normalize_invoice_no,
)


@pytest.mark.parametrize("value", ["INV-001", "inv 001", " INV-001 ", "INV/001"])
def test_invoice_number_variants_share_normalized_key(value):
    assert normalize_invoice_no(value) == "INV001"


def test_invoice_number_normalization_uses_nfkc_and_keeps_leading_zeroes():
    assert normalize_invoice_no("ＩＮＶ-００１") == "INV001"
    assert normalize_invoice_no("INV-0001") != normalize_invoice_no("INV-001")


def test_invoice_number_normalization_removes_unicode_punctuation():
    assert normalize_invoice_no(" inv—001 ") == "INV001"


def test_duplicate_lookup_is_scoped_to_vendor_and_current_invoice():
    vendor_id = uuid.uuid4()
    duplicate = object()
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value.first.return_value = duplicate
    db = MagicMock()
    db.query.return_value = query

    result = find_duplicate_invoice(db, vendor_id, "INV001")

    assert result is duplicate
    query.filter.assert_called_once()
    query.with_for_update.assert_called_once()


def test_current_invoice_number_unique_index_uses_normalized_key():
    index = next(
        item
        for item in Invoice.__table__.indexes
        if item.name == "uq_current_vendor_invoice_normalized"
    )

    assert isinstance(index, Index)
    assert [column.name for column in index.columns] == [
        "vendor_id",
        "invoice_no_normalized",
    ]
    assert index.unique


def test_invoice_mapper_hooks_synchronize_normalized_key_on_insert_and_update():
    invoice = Invoice(invoice_no="INV-001", invoice_no_normalized="WRONG")

    _set_invoice_number_key(None, None, invoice)
    assert invoice.invoice_no_normalized == "INV001"

    invoice.invoice_no = "inv 002"
    _set_invoice_number_key(None, None, invoice)
    assert invoice.invoice_no_normalized == "INV002"
