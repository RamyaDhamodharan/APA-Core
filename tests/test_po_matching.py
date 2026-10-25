import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.po_matching_services import (
    match_invoice_to_po,
    normalize_amount,
    normalize_value,
)


def make_document(vendor, order_number, total):
    return SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="po",
        extracted_json={
            "vendor": vendor,
            "order_number": order_number,
            "total": total,
        },
    )


def setup_db(po):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [po]
    return db


def test_normalize_value():
    assert normalize_value("  ABC   Supplies ") == "abc supplies"


def test_normalize_amount():
    assert normalize_amount("1,500.00") == 1500
    assert normalize_amount(None) is None
    assert normalize_amount("invalid") is None


def test_matching_po_returns_success():
    po = make_document("ABC Supplies", "PO-101", "1500.00")
    db = setup_db(po)

    result = match_invoice_to_po(
        db,
        {
            "vendor": " abc supplies ",
            "order_number": "po-101",
            "total": 1500,
        },
        uuid.uuid4(),
    )

    assert result["matched"] is True
    assert result["po_document_id"] == str(po.id)


def test_vendor_mismatch_requires_review():
    po = make_document("ABC Supplies", "PO-101", 1500)
    db = setup_db(po)

    result = match_invoice_to_po(
        db,
        {
            "vendor": "XYZ Traders",
            "order_number": "PO-101",
            "total": 1500,
        },
        uuid.uuid4(),
    )

    assert result["matched"] is False
    assert "vendor" in result["reason"].lower()


def test_total_mismatch_requires_review():
    po = make_document("ABC Supplies", "PO-101", 1500)
    db = setup_db(po)

    result = match_invoice_to_po(
        db,
        {
            "vendor": "ABC Supplies",
            "order_number": "PO-101",
            "total": 1800,
        },
        uuid.uuid4(),
    )

    assert result["matched"] is False
    assert "total" in result["reason"].lower()


def test_missing_po_returns_not_found():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []

    result = match_invoice_to_po(
        db,
        {
            "vendor": "ABC Supplies",
            "order_number": "PO-999",
            "total": 1500,
        },
        uuid.uuid4(),
    )

    assert result["matched"] is False
    assert result["po_document_id"] is None


def test_missing_order_number_skips_database_query():
    db = MagicMock()

    result = match_invoice_to_po(
        db,
        {
            "vendor": "ABC Supplies",
            "order_number": "",
            "total": 1500,
        },
        uuid.uuid4(),
    )

    assert result["matched"] is False
    assert "order number" in result["reason"].lower()
    db.query.assert_not_called()
