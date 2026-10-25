import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.vendor_models import Vendor

client = TestClient(app)


def _setup(db=None):
    db = db or MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "role": "admin",
    }

    def teardown():
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return db, teardown


def _vendor(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Acme Supplies",
        "tax_id": None,
        "is_active": True,
        "contact_name": None,
        "contact_email": None,
        "contact_phone": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_vendor_normalizes_and_persists_fields():
    db, teardown = _setup()
    db.scalars.return_value.first.return_value = None
    db.refresh.side_effect = lambda vendor: (
        setattr(vendor, "id", uuid.uuid4()),
        setattr(vendor, "is_active", True),
    )

    try:
        response = client.post(
            "/api/vendors",
            json={
                "name": "  Acme Supplies  ",
                "tax_id": " TX-123 ",
                "contact_name": " Jane Doe ",
                "contact_email": "jane@example.com",
                "contact_phone": " 555-0100 ",
            },
        )
    finally:
        teardown()

    assert response.status_code == 201
    assert response.json()["name"] == "Acme Supplies"
    assert response.json()["tax_id"] == "TX-123"
    assert response.json()["contact_name"] == "Jane Doe"
    assert response.json()["contact_email"] == "jane@example.com"
    assert response.json()["contact_phone"] == "555-0100"
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    (
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "Valid", "contact_email": "not-an-email"},
        {"name": "Valid", "tax_id": "x" * 31},
        {"name": "Valid", "contact_name": "x" * 121},
        {"name": "Valid", "contact_phone": "x" * 41},
        {"name": "Valid", "contact_email": f"{'x' * 190}@example.com"},
    ),
)
def test_create_vendor_rejects_invalid_fields(payload):
    db, teardown = _setup()
    try:
        response = client.post("/api/vendors", json=payload)
    finally:
        teardown()

    assert response.status_code == 422
    db.scalars.assert_not_called()
    db.commit.assert_not_called()


def test_create_vendor_rejects_case_insensitive_duplicate():
    db, teardown = _setup()
    db.query.return_value.filter.return_value.first.return_value = _vendor()

    try:
        response = client.post(
            "/api/vendors",
            json={"name": "  ACME SUPPLIES "},
        )
    finally:
        teardown()

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_create_vendor_rolls_back_database_conflict():
    db, teardown = _setup()
    db.scalars.return_value.first.return_value = None
    db.commit.side_effect = IntegrityError(
        "insert",
        {},
        Exception("constraint violation"),
    )

    try:
        response = client.post(
            "/api/vendors",
            json={"name": "Acme Supplies"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    assert response.json()["detail"] == ("Vendor conflicts with existing data")
    db.rollback.assert_called_once()


def test_update_vendor_rolls_back_database_conflict():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor
    db.commit.side_effect = IntegrityError(
        "update",
        {},
        Exception("constraint violation"),
    )

    try:
        response = client.patch(
            f"/api/vendors/{vendor.id}",
            json={"contact_email": "billing@example.com"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    db.rollback.assert_called_once()


def test_get_vendor_returns_existing_vendor():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor

    try:
        response = client.get(f"/api/vendors/{vendor.id}")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["id"] == str(vendor.id)
    assert response.json()["name"] == vendor.name
    db.get.assert_called_once_with(Vendor, vendor.id)


def test_delete_vendor_deactivates_without_removing_record():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor

    try:
        response = client.delete(f"/api/vendors/{vendor.id}")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert vendor.is_active is False
    db.delete.assert_not_called()
    db.commit.assert_called_once()


@pytest.mark.parametrize("method", ("get", "patch"))
def test_vendor_detail_and_update_return_not_found(method):
    db, teardown = _setup()
    db.get.return_value = None

    try:
        if method == "get":
            response = client.get(f"/api/vendors/{uuid.uuid4()}")
        else:
            response = client.patch(
                f"/api/vendors/{uuid.uuid4()}",
                json={"name": "Renamed Vendor"},
            )
    finally:
        teardown()

    assert response.status_code == 404
    assert response.json()["detail"] == "Vendor not found"
    db.commit.assert_not_called()


def test_update_vendor_changes_only_provided_fields():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor

    try:
        response = client.patch(
            f"/api/vendors/{vendor.id}",
            json={
                "contact_email": "billing@example.com",
                "is_active": False,
            },
        )
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["name"] == "Acme Supplies"
    assert response.json()["contact_email"] == "billing@example.com"
    assert response.json()["is_active"] is False
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(vendor)


def test_update_vendor_rejects_blank_name_and_empty_patch():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor

    try:
        blank_response = client.patch(
            f"/api/vendors/{vendor.id}",
            json={"name": " "},
        )
        empty_response = client.patch(
            f"/api/vendors/{vendor.id}",
            json={},
        )
    finally:
        teardown()

    assert blank_response.status_code == 422
    assert empty_response.status_code == 422
    db.commit.assert_not_called()


def test_update_vendor_rejects_null_active_status():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor

    try:
        response = client.patch(
            f"/api/vendors/{vendor.id}",
            json={"is_active": None},
        )
    finally:
        teardown()

    assert response.status_code == 422
    db.commit.assert_not_called()


def test_update_vendor_rejects_duplicate_name():
    vendor = _vendor()
    db, teardown = _setup()
    db.get.return_value = vendor
    db.query.return_value.filter.return_value.first.return_value = _vendor(
        id=uuid.uuid4(),
        name="Other Vendor",
    )

    try:
        response = client.patch(
            f"/api/vendors/{vendor.id}",
            json={"name": "Other Vendor"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    db.commit.assert_not_called()


def test_vendor_list_preserves_array_response_and_supports_search_pagination():
    vendors = [_vendor()]
    db, teardown = _setup()
    db.scalars.return_value.all.return_value = vendors

    try:
        response = client.get("/api/vendors?search=Acme&page=2&page_size=5")
    finally:
        teardown()

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["name"] == "Acme Supplies"
    statement = db.scalars.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "vendors.name ILIKE" in str(compiled)
    assert "%Acme%" in compiled.params.values()
    assert "LIMIT" in str(compiled)
    assert "OFFSET" in str(compiled)
    assert 5 in compiled.params.values()


def test_vendor_list_without_parameters_preserves_unpaginated_array():
    db, teardown = _setup()
    db.scalars.return_value.all.return_value = [_vendor()]

    try:
        response = client.get("/api/vendors")
    finally:
        teardown()

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    statement = db.scalars.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "LIMIT" not in str(compiled)
    assert "OFFSET" not in str(compiled)


def test_vendor_list_rejects_invalid_pagination_and_blank_search():
    db, teardown = _setup()
    try:
        invalid_responses = (
            client.get("/api/vendors?page=0"),
            client.get("/api/vendors?page_size=101"),
            client.get("/api/vendors?search=%20%20"),
        )
    finally:
        teardown()

    assert all(response.status_code == 422 for response in invalid_responses)
    db.scalars.assert_not_called()


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    (
        ("get", "/api/vendors", None),
        ("get", f"/api/vendors/{uuid.uuid4()}", None),
        ("post", "/api/vendors", {"name": "Acme"}),
        (
            "patch",
            f"/api/vendors/{uuid.uuid4()}",
            {"contact_phone": "555-0100"},
        ),
    ),
)
def test_vendor_routes_require_authentication(method, path, json_body):
    db, teardown = _setup()
    app.dependency_overrides.pop(get_current_user, None)

    try:
        request = getattr(client, method)
        response = request(path) if json_body is None else request(path, json=json_body)
    finally:
        teardown()

    assert response.status_code == 401
    db.scalars.assert_not_called()
    db.get.assert_not_called()
