import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.db.session_db import get_db
from src.models.user_models import User
from src.models.vendor_models import Vendor

client = TestClient(app)


def _token(monkeypatch, role, user_id=None):
    secret = "rbac-test-secret-with-at-least-32-bytes"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    return jwt.encode(
        {"sub": str(user_id or uuid.uuid4()), "type": "access", "role": role},
        secret,
        algorithm="HS256",
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "patch",
            "/api/vendors/00000000-0000-0000-0000-000000000001",
            {"name": "Vendor"},
        ),
        (
            "post",
            "/api/vendors/00000000-0000-0000-0000-000000000001/aliases",
            {"alias": "Alt"},
        ),
        (
            "patch",
            "/api/vendors/00000000-0000-0000-0000-000000000001/aliases/00000000-0000-0000-0000-000000000002",
            {"alias": "Alt"},
        ),
        (
            "delete",
            "/api/vendors/00000000-0000-0000-0000-000000000001/aliases/00000000-0000-0000-0000-000000000002",
            None,
        ),
        ("delete", "/api/vendors/00000000-0000-0000-0000-000000000001", None),
        (
            "patch",
            "/api/purchase-orders/00000000-0000-0000-0000-000000000001/vendor",
            {"vendor_id": "00000000-0000-0000-0000-000000000002"},
        ),
    ],
)
def test_associate_token_cannot_mutate_vendors(
    monkeypatch, method, path, body, user_stub_factory
):
    user = user_stub_factory(role="associate")
    db = MagicMock()
    db.get.side_effect = lambda model, _id: user if model is User else None
    app.dependency_overrides[get_db] = lambda: db
    headers = {
        "Authorization": f"Bearer {_token(monkeypatch, 'associate', user.id)}"
    }
    try:
        response = (
            client.request("DELETE", path, headers=headers)
            if method == "delete"
            else getattr(client, method)(path, json=body, headers=headers)
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 403


def test_missing_token_cannot_create_vendor():
    response = client.post("/api/vendors", json={"name": "Vendor"})
    assert response.status_code in {401, 403}


def test_admin_token_can_create_vendor(monkeypatch, user_stub):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    (
        db.query.return_value.filter.return_value.filter.return_value.first.return_value
    ) = None
    db.scalars.return_value.first.return_value = None

    def refresh(vendor):
        vendor.id = uuid.uuid4()
        vendor.is_active = True

    db.refresh.side_effect = refresh
    db.get.side_effect = lambda model, _id: user_stub if model is User else None
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = client.post(
            "/api/vendors",
            json={"name": "Vendor"},
            headers={
                "Authorization": f"Bearer {_token(monkeypatch, 'admin', user_stub.id)}"
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 201


def test_associate_token_can_create_vendor(monkeypatch, user_stub):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    (
        db.query.return_value.filter.return_value.filter.return_value.first.return_value
    ) = None
    db.scalars.return_value.first.return_value = None

    def refresh(vendor):
        vendor.id = uuid.uuid4()
        vendor.is_active = True

    db.refresh.side_effect = refresh
    db.get.side_effect = lambda model, _id: user_stub if model is User else None
    app.dependency_overrides[get_db] = lambda: db
    headers = {
        "Authorization": f"Bearer {_token(monkeypatch, 'associate', user_stub.id)}"
    }
    try:
        response = client.post(
            "/api/vendors",
            json={"name": "Associate vendor"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 201


def test_admin_token_can_create_edit_and_delete_vendor_aliases(
    monkeypatch, user_stub
):
    vendor_id = uuid.uuid4()
    alias_id = uuid.uuid4()
    vendor = type("VendorRow", (), {"id": vendor_id, "is_active": True})()
    now = datetime.now(UTC)
    alias = type(
        "AliasRow",
        (),
        {
            "id": alias_id,
            "vendor_id": vendor_id,
            "alias_norm": "alt vendor",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    )()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    (
        db.query.return_value.filter.return_value.filter.return_value.first.return_value
    ) = None
    db.get.side_effect = lambda model, _id: (
        user_stub
        if model is User
        else vendor
        if model is Vendor
        else alias
    )

    def refresh(row):
        row.id = getattr(row, "id", None) or alias_id
        row.created_at = getattr(row, "created_at", None) or now
        row.updated_at = getattr(row, "updated_at", None) or now

    db.refresh.side_effect = refresh
    app.dependency_overrides[get_db] = lambda: db
    headers = {
        "Authorization": f"Bearer {_token(monkeypatch, 'admin', user_stub.id)}"
    }
    try:
        created = client.post(
            f"/api/vendors/{vendor_id}/aliases",
            json={"alias": "alt vendor"},
            headers=headers,
        )
        edited = client.patch(
            f"/api/vendors/{vendor_id}/aliases/{alias_id}",
            json={"alias": "new alternate"},
            headers=headers,
        )
        deleted = client.delete(
            f"/api/vendors/{vendor_id}/aliases/{alias_id}", headers=headers
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert created.status_code == 201
    assert edited.status_code == 200, edited.text
    assert deleted.status_code == 200
