import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import document_routers
from src.api.rest.routers.purchase_order_routers import reassign_purchase_order_vendor
from src.db.session_db import get_db
from src.models.document_models import Document
from src.models.purchase_order_models import PurchaseOrder
from src.schemas.purchase_order_schemas import (
    PurchaseOrderVendorReassignmentRequest,
    PurchaseOrderVendorResolutionRequest,
)

client = TestClient(app)


def _override(db, owner_id):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}


def _clear_overrides():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_purchase_order_list_returns_saved_headers_and_lines(monkeypatch):
    owner_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    now = datetime.now(UTC)
    order = SimpleNamespace(
        id=uuid.uuid4(),
        po_number="PO-2026-100",
        version_no=1,
        is_current=True,
        vendor_id=vendor_id,
        vendor_name="Existing Vendor",
        source_document_id=uuid.uuid4(),
        po_date=date(2026, 10, 1),
        currency="USD",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("10.00"),
        total_amount=Decimal("110.00"),
        payment_terms="Net 30",
        delivery_details={"address": "Dock 3"},
        created_at=now,
        updated_at=now,
        lines=[],
    )
    db = MagicMock()
    db.scalar.return_value = 1
    db.scalars.return_value.all.return_value = [order]
    _override(db, owner_id)

    try:
        response = client.get("/api/purchase-orders")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["po_number"] == order.po_number
    assert response.json()["items"][0]["payment_terms"] == "Net 30"
    assert response.json()["items"][0]["delivery_details"] == {"address": "Dock 3"}
    compiled_query = str(db.scalar.call_args.args[0])
    assert "purchase_orders.created_by" in compiled_query
    assert "documents.created_by" in compiled_query


def test_purchase_order_detail_hides_another_uploaders_order():
    db = MagicMock()
    db.scalar.return_value = None
    _override(db, uuid.uuid4())

    try:
        response = client.get(f"/api/purchase-orders/{uuid.uuid4()}")
    finally:
        _clear_overrides()

    assert response.status_code == 404


def test_reassign_purchase_order_vendor_updates_owner_scoped_record():
    owner_id, po_id, old_vendor_id, vendor_id = (
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    )
    order = SimpleNamespace(id=po_id, vendor_id=old_vendor_id)
    vendor = SimpleNamespace(id=vendor_id, name="New Vendor", is_active=True)
    db = MagicMock()
    db.scalar.return_value = order
    db.get.return_value = vendor

    result = reassign_purchase_order_vendor(
        po_id,
        PurchaseOrderVendorReassignmentRequest(vendor_id=vendor_id),
        db,
        {"sub": str(owner_id), "role": "admin"},
    )

    assert result is order
    assert order.vendor_id == vendor_id
    assert "purchase_orders.created_by" in str(db.scalar.call_args.args[0])
    db.commit.assert_called_once()


def test_resolve_po_vendor_persists_selected_vendor_atomically(monkeypatch):
    owner_id = uuid.uuid4()
    vendor = SimpleNamespace(
        id=uuid.uuid4(),
        name="Existing Vendor",
        is_active=True,
    )
    now = datetime.now(UTC)
    document = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="po.pdf",
        status="needs_vendor_review",
        doc_type="po",
        failure_message="Vendor not found",
        extracted_json={
            "po_number": "PO-2026-100",
            "po_date": "2026-10-01",
            "currency": "USD",
            "subtotal": "100.00",
            "tax": "10.00",
            "total": "110.00",
            "line_items": [],
        },
        created_at=now,
        updated_at=now,
    )
    po_query = MagicMock()
    po_query.filter.return_value.first.return_value = None
    document_query = MagicMock()
    (
        document_query.filter.return_value.with_for_update.return_value.first.return_value
    ) = document
    db = MagicMock()
    db.query.side_effect = lambda model: {
        Document: document_query,
        PurchaseOrder: po_query,
    }[model]
    db.get.return_value = vendor
    purchase_order = SimpleNamespace(
        id=uuid.uuid4(),
        po_number="PO-2026-100",
        version_no=1,
        is_current=True,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        source_document_id=document.id,
        po_date=date(2026, 10, 1),
        currency="USD",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("10.00"),
        total_amount=Decimal("110.00"),
        payment_terms=None,
        delivery_details=None,
        created_at=now,
        updated_at=now,
        lines=[],
    )
    persisted = []
    monkeypatch.setattr(
        document_routers,
        "_save_purchase_order_from_extraction",
        lambda _db, _document, data, selected_vendor: (
            persisted.append((data, selected_vendor)) or purchase_order
        ),
    )
    _override(db, owner_id)

    try:
        response = client.post(
            f"/api/documents/{document.id}/resolve-po-vendor",
            json={"vendor_id": str(vendor.id)},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["purchase_order"]["po_number"] == "PO-2026-100"
    assert persisted[0][0]["vendor"] == vendor.name
    assert persisted[0][0]["vendor_resolution"]["method"] == "user_confirmed"
    assert document.status == "extracted"
    assert document.failure_message is None
    db.commit.assert_called_once()


def test_vendor_resolution_request_requires_exactly_one_choice():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PurchaseOrderVendorResolutionRequest()
    with pytest.raises(ValidationError):
        PurchaseOrderVendorResolutionRequest(
            vendor_id=uuid.uuid4(), new_vendor={"name": "Vendor"}
        )
