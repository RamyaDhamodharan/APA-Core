import os
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import invoice_routers
from src.api.rest.routers.document_routers import (
    _save_goods_receipt_from_extraction,
)
from src.api.rest.routers.goods_receipt_routers import (
    _handle_goods_receipt_integrity_error,
)
from src.db.session_db import get_db
from src.services.po_matching_services import match_invoice_three_way

client = TestClient(app)


def _query(rows=(), first=None):
    query = MagicMock()
    query.options.return_value = query
    query.filter.return_value = query
    query.all.return_value = list(rows)
    query.first.return_value = first
    return query


def _case(
    *,
    receipt=True,
    accepted="10",
    received="10",
    invoice_qty="10",
    vendor_mismatch=False,
    po_mismatch=False,
    duplicate=False,
):
    po_id, vendor_id, po_line_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    po = SimpleNamespace(
        id=po_id,
        vendor_id=uuid.uuid4() if vendor_mismatch else vendor_id,
        po_number="PO-10",
        currency="USD",
        total_amount=Decimal("100"),
    )
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_no="INV-10",
        vendor_id=vendor_id,
        po_number_raw="PO-OTHER" if po_mismatch else "PO-10",
        currency="USD",
        total_amount=Decimal("100"),
        po_id=po_id,
    )
    po_line = SimpleNamespace(
        id=po_line_id,
        item_code="A",
        description="Item A",
        qty=Decimal("10"),
        unit_price=Decimal("10"),
    )
    invoice_line = SimpleNamespace(
        id=uuid.uuid4(),
        item_code="A",
        description="Item A",
        qty=Decimal(invoice_qty),
        unit_price=Decimal("10"),
        line_amount=Decimal(invoice_qty) * Decimal("10"),
        po_line_id=po_line_id,
    )
    receipt_line = SimpleNamespace(
        po_line_id=po_line_id,
        accepted_qty=Decimal(accepted),
        received_qty=Decimal(received),
    )
    receipts = (
        [SimpleNamespace(id=uuid.uuid4(), vendor_id=vendor_id, lines=[receipt_line])]
        if receipt
        else []
    )
    queries = [
        _query(first=object() if duplicate else None),
        _query(receipts),
        _query([po_line]),
        _query([invoice_line]),
    ]
    db = MagicMock()
    db.get.return_value = po
    db.query.side_effect = lambda model: queries.pop(0)
    return db, invoice, receipts


def test_three_way_match_success():
    db, invoice, _ = _case()
    result = match_invoice_three_way(db, invoice)
    assert result["matched"] is True
    assert result["requires_review"] is True
    assert result["issues"] == []


def test_three_way_match_missing_goods_receipt():
    db, invoice, _ = _case(receipt=False)
    result = match_invoice_three_way(db, invoice)
    assert result["matched"] is False
    assert any("No goods receipt" in issue for issue in result["issues"])


def test_three_way_match_partial_receipt_requires_review():
    db, invoice, _ = _case(accepted="5", received="5", invoice_qty="4")
    result = match_invoice_three_way(db, invoice)
    assert result["matched"] is False
    assert any("Partial receipt" in issue for issue in result["issues"])


def test_three_way_match_invoice_quantity_exceeds_accepted():
    db, invoice, _ = _case(accepted="5", received="5", invoice_qty="6")
    result = match_invoice_three_way(db, invoice)
    assert any("exceeds accepted" in issue for issue in result["issues"])


def test_three_way_match_detects_over_receipt():
    db, invoice, _ = _case(received="11", accepted="11")
    result = match_invoice_three_way(db, invoice)
    assert any("Over-receipt" in issue for issue in result["issues"])


def test_three_way_match_vendor_and_po_mismatch():
    db, invoice, _ = _case(vendor_mismatch=True, po_mismatch=True)
    result = match_invoice_three_way(db, invoice)
    assert any("vendor does not match" in issue for issue in result["issues"])
    assert any("PO number" in issue for issue in result["issues"])


def test_three_way_match_detects_duplicate_invoice():
    db, invoice, _ = _case(duplicate=True)
    result = match_invoice_three_way(db, invoice)
    assert any("Duplicate invoice" in issue for issue in result["issues"])


@pytest.mark.skipif(
    os.getenv("ENABLE_GOODS_RECEIPT", "false").casefold()
    not in {"1", "true", "yes", "on"},
    reason="goods receipt API is disabled by default",
)
def test_create_goods_receipt_rejects_duplicate_number():
    user_id = uuid.uuid4()
    db = MagicMock()
    db.scalar.return_value = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id)}
    try:
        response = client.post(
            "/api/goods-receipts",
            json={
                "gr_number": "GR-10",
                "po_id": str(uuid.uuid4()),
                "vendor_id": str(uuid.uuid4()),
                "receipt_date": "2026-10-01",
                "lines": [
                    {
                        "po_line_id": str(uuid.uuid4()),
                        "received_qty": "1",
                        "accepted_qty": "1",
                    }
                ],
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 409


def _extraction_case(
    *, duplicate=False, vendor_mismatch=False, po_lines=None, po_candidates=None
):
    po_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    po = SimpleNamespace(
        id=po_id,
        vendor_id=uuid.uuid4() if vendor_mismatch else vendor_id,
    )
    default_line = SimpleNamespace(
        id=uuid.uuid4(),
        item_code="SKU-1",
        description="Item 1",
        uom="EA",
    )
    po_lines = po_lines if po_lines is not None else [default_line]
    queries = [
        _query(first=None),
        _query(first=object() if duplicate else None),
        _query(po_candidates if po_candidates is not None else [po]),
        _query(po_lines),
    ]
    db = MagicMock()
    db.query.side_effect = lambda model: queries.pop(0)
    data = {
        "gr_number": "GR-20",
        "order_number": "PO-20",
        "receipt_date": "2026-10-10",
        "line_items": [
            {
                "item_code": "SKU-1",
                "description": "Item 1",
                "received_qty": "5",
                "accepted_qty": "4",
                "rejected_qty": "1",
            }
        ],
    }
    return (
        db,
        SimpleNamespace(id=uuid.uuid4()),
        data,
        SimpleNamespace(id=vendor_id),
        default_line,
    )


@pytest.mark.parametrize("gr_number", [None, "", "   "])
def test_goods_receipt_extraction_rejects_missing_or_blank_number(gr_number):
    db, document, data, vendor, _ = _extraction_case()
    data["gr_number"] = gr_number
    with pytest.raises(ValueError, match="Goods receipt number is required"):
        _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.query.assert_not_called()
    db.add.assert_not_called()


def test_goods_receipt_extraction_rejects_duplicate_number():
    db, document, data, vendor, _ = _extraction_case(duplicate=True)
    with pytest.raises(ValueError, match="Duplicate goods receipt number"):
        _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_not_called()


def test_goods_receipt_extraction_persists_without_ambiguous_purchase_order():
    po_id = uuid.uuid4()
    candidates = [
        SimpleNamespace(id=po_id, vendor_id=uuid.uuid4()),
        SimpleNamespace(id=uuid.uuid4(), vendor_id=uuid.uuid4()),
    ]
    db, document, data, vendor, _ = _extraction_case(po_candidates=candidates)

    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


def test_goods_receipt_extraction_skips_malformed_line_but_persists_header():
    db, document, data, vendor, _ = _extraction_case()
    data["line_items"] = [None]
    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


@pytest.mark.parametrize(
    "constraint_name",
    ["uq_goods_receipts_gr_number", "uq_goods_receipts_gr_number_norm"],
)
def test_goods_receipt_duplicate_constraint_maps_to_conflict(constraint_name):
    db = MagicMock()
    exc = IntegrityError(
        "insert",
        {},
        SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint_name)),
    )
    with pytest.raises(HTTPException) as raised:
        _handle_goods_receipt_integrity_error(db, exc)
    assert raised.value.status_code == 409
    db.rollback.assert_called_once()


def test_unrelated_goods_receipt_integrity_error_is_reraised():
    db = MagicMock()
    exc = IntegrityError(
        "insert", {}, SimpleNamespace(diag=SimpleNamespace(constraint_name="fk_users"))
    )
    with pytest.raises(IntegrityError) as raised:
        _handle_goods_receipt_integrity_error(db, exc)
    assert raised.value is exc
    db.rollback.assert_called_once()


@pytest.mark.skipif(
    os.getenv("ENABLE_GOODS_RECEIPT", "false").casefold()
    not in {"1", "true", "yes", "on"},
    reason="goods receipt API is disabled by default",
)
def test_goods_receipt_update_rejects_empty_lines_without_mutating_receipt():
    existing_line = SimpleNamespace(id=uuid.uuid4())
    receipt = SimpleNamespace(lines=[existing_line])
    db = MagicMock()
    db.scalar.return_value = receipt
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}
    try:
        response = client.patch(
            f"/api/goods-receipts/{uuid.uuid4()}", json={"lines": []}
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 422
    assert receipt.lines == [existing_line]
    db.scalar.assert_not_called()
    db.flush.assert_not_called()


def test_goods_receipt_extraction_persists_vendor_po_mismatch_for_review():
    db, document, data, vendor, _ = _extraction_case(vendor_mismatch=True)
    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


@pytest.mark.parametrize(
    ("received", "accepted", "rejected"),
    [("0", "0", "0"), ("5", "4", "0"), ("5", "-1", "6")],
)
def test_goods_receipt_extraction_persists_invalid_quantities_for_review(
    received, accepted, rejected
):
    db, document, data, vendor, _ = _extraction_case()
    data["line_items"][0].update(
        received_qty=received,
        accepted_qty=accepted,
        rejected_qty=rejected,
    )
    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


def test_goods_receipt_extraction_persists_with_unmatched_ambiguous_po_line():
    line = SimpleNamespace(
        id=uuid.uuid4(),
        item_code="SKU-1",
        description="Item 1",
        uom="EA",
    )
    db, document, data, vendor, line = _extraction_case(
        po_lines=[
            line,
            SimpleNamespace(
                id=uuid.uuid4(),
                item_code="SKU-1",
                description="Alternate item row",
                uom="EA",
            ),
        ]
    )
    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


def test_goods_receipt_extraction_persists_with_unmatched_po_line_id():
    db, document, data, vendor, _ = _extraction_case()
    data["line_items"][0]["po_line_id"] = str(uuid.uuid4())
    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


def test_goods_receipt_extraction_persists_po_line_id_without_item_evidence():
    db, document, data, vendor, _ = _extraction_case()
    data["line_items"][0].pop("item_code")
    data["line_items"][0].pop("description")
    data["line_items"][0]["po_line_id"] = str(uuid.uuid4())

    _save_goods_receipt_from_extraction(db, document, data, vendor)
    db.add.assert_called_once()


def test_goods_receipt_extraction_rejects_incompatible_unit():
    db, document, data, vendor, _ = _extraction_case()
    data["line_items"][0]["uom"] = "CASE"

    with pytest.raises(ValueError, match="unit does not match its PO line"):
        _save_goods_receipt_from_extraction(db, document, data, vendor)

    db.add.assert_not_called()


def test_three_way_match_endpoint_does_not_change_invoice_approval(monkeypatch):
    db, invoice, _ = _case()
    invoice.final_status = "approved"
    (
        db.query.return_value.outerjoin.return_value.filter.return_value.first.return_value
    ) = invoice
    recommendation = SimpleNamespace(
        matching_status="matched",
        human_review_required=False,
        next_recommended_action="Submit for human approval",
        checks=[],
        supporting_records=[],
        model_dump=lambda mode=None: {
            "invoice_id": str(invoice.id),
            "processing_status": "extracted",
            "matching_status": "matched",
            "review_status": "ready_for_human_review",
            "approval_status": "approved",
            "payment_status": "unknown",
            "supporting_records": [],
            "missing_document_types": [],
            "candidates": [],
            "checks": [],
            "discrepancies": [],
            "reason_codes": [],
            "next_recommended_action": "Submit for human approval",
            "human_review_required": False,
            "invoice_approval_required": True,
        },
    )
    monkeypatch.setattr(
        invoice_routers,
        "recommend_invoice_review",
        lambda _db, _invoice, owner_id: recommendation,
    )
    monkeypatch.setattr(
        invoice_routers,
        "run_match_for_invoice",
        lambda *_args, **_kwargs: SimpleNamespace(
            id=uuid.uuid4(),
            rules_used={
                "match_outcome": {
                    "outcome": "ready_to_pay",
                    "rule_set_version": "ap-match-v1",
                    "discrepancies": [],
                }
            },
        ),
    )
    db.add = MagicMock()
    user_id = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id)}
    try:
        response = client.post(f"/api/invoices/{invoice.id}/three-way-match")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200
    assert response.json()["matched"] is True
    assert invoice.final_status == "approved"
    db.commit.assert_called_once()
