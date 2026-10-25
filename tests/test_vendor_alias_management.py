import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers.vendor_routers import _clear_stale_vendor_flag
from src.db.session_db import get_db
from src.models.vendor_models import VendorAlias
from src.services.vendor_resolution_services import (
    normalize_vendor_name,
    resolve_vendor,
    suggest_vendor_candidates,
)

client = TestClient(app)


def _setup(db=None):
    db = db or MagicMock()
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


def _vendor(*, name="Canonical Vendor", is_active=True, vendor_id=None):
    return SimpleNamespace(
        id=vendor_id or uuid.uuid4(),
        name=name,
        is_active=is_active,
    )


def _alias(
    vendor_id,
    *,
    name="alternate vendor",
    is_active=True,
    alias_id=None,
):
    return SimpleNamespace(
        id=alias_id or uuid.uuid4(),
        vendor_id=vendor_id,
        alias_norm=name,
        is_active=is_active,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
        updated_at=datetime(2026, 10, 19, tzinfo=UTC),
    )


def _resolver_db(canonical_vendors=(), alias_rows=()):
    canonical_query = MagicMock()
    canonical_query.filter.return_value.all.return_value = list(canonical_vendors)
    alias_query = MagicMock()
    alias_query.join.return_value.filter.return_value.all.return_value = list(
        alias_rows
    )
    db = MagicMock()
    db.query.side_effect = [alias_query, canonical_query]
    return db, canonical_query, alias_query


def test_vendor_name_normalization_collapses_whitespace_and_case():
    assert normalize_vendor_name("  ACME \t  SUPPLIES \n") == ("acme supplies")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("QuickBasket Traders, Pvt. Ltd.", "quickbasket traders"),
        ("Northwind Private LLP", "northwind"),
        ("Acme & Co", "acme"),
    ],
)
def test_vendor_name_normalization_strips_punctuation_and_company_suffixes(
    raw, expected
):
    assert normalize_vendor_name(raw) == expected


def test_resolution_prefers_exact_alias_over_canonical_name():
    canonical = _vendor(name="Acme Supplies")
    alias_vendor = _vendor(name="Other Canonical")
    alias = _alias(alias_vendor.id, name="acme supplies")
    db, _, alias_query = _resolver_db(
        canonical_vendors=[canonical],
        alias_rows=[(alias, alias_vendor)],
    )

    assert resolve_vendor(db, "  ACME   SUPPLIES ") is alias_vendor
    alias_query.join.assert_called_once()


def test_resolution_accepts_case_and_whitespace_variations():
    canonical = _vendor(name="Acme Supplies")
    db, _, _ = _resolver_db(canonical_vendors=[canonical])

    assert resolve_vendor(db, "  aCmE\t supplies ") is canonical


def test_resolution_uses_unique_alias_mapping():
    vendor = _vendor()
    alias = _alias(vendor.id)
    db, _, _ = _resolver_db(alias_rows=[(alias, vendor)])

    assert resolve_vendor(db, "  ALTERNATE   VENDOR ") is vendor


def test_resolution_leaves_unknown_vendor_unresolved():
    db, _, _ = _resolver_db()

    assert resolve_vendor(db, "Unknown Vendor") is None


def test_resolution_leaves_ambiguous_alias_unresolved():
    first_vendor = _vendor(name="First")
    second_vendor = _vendor(name="Second")
    first_alias = _alias(first_vendor.id)
    second_alias = _alias(second_vendor.id)
    db, _, _ = _resolver_db(
        alias_rows=[
            (first_alias, first_vendor),
            (second_alias, second_vendor),
        ]
    )

    assert resolve_vendor(db, "alternate vendor") is None


def test_resolution_filters_out_inactive_aliases_and_vendors():
    alias_query = MagicMock()
    alias_query.join.return_value.filter.return_value.all.return_value = []
    canonical_query = MagicMock()
    canonical_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [alias_query, canonical_query]

    assert resolve_vendor(db, "alternate vendor") is None

    compiled = [
        expression.compile(dialect=postgresql.dialect())
        for expression in alias_query.join.return_value.filter.call_args.args
    ]
    assert any(
        "vendor_aliases.is_active IS true" in str(expression) for expression in compiled
    )
    assert any(
        "vendors.is_active IS true" in str(expression) for expression in compiled
    )


def test_resolution_uses_unique_active_tax_id_before_vendor_name():
    vendor = _vendor(name="Canonical Vendor")
    tax_query = MagicMock()
    tax_query.filter.return_value.all.return_value = [vendor]
    db = MagicMock()
    db.query.return_value = tax_query

    assert (
        resolve_vendor(db, "Different extracted spelling", tax_id="US-12 34") is vendor
    )
    db.query.assert_called_once()


def test_unmatched_tax_id_does_not_fall_back_to_similar_vendor_name():
    tax_query = MagicMock()
    tax_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.return_value = tax_query

    assert resolve_vendor(db, "Canonical Vendor", tax_id="MISMATCH") is None
    db.query.assert_called_once()


def test_fuzzy_match_is_only_a_suggestion():
    candidate = _vendor(name="QuickBasket Traders")
    db = MagicMock()
    query = MagicMock()
    query.filter.return_value.all.return_value = [candidate]
    db.query.return_value = query

    assert resolve_vendor(db, "Quick Basket Traderz") is None
    assert suggest_vendor_candidates(db, "Quick Basket Traderz", threshold=0.8) == [
        candidate
    ]


def test_exact_resolution_clears_only_stale_new_vendor_flag():
    document = SimpleNamespace(
        status="needs_vendor_review",
        failure_message="New vendor. Add or select the vendor to continue matching.",
    )
    _clear_stale_vendor_flag(document)
    assert document.failure_message is None
    assert document.status == "needs_data_review"

    other = SimpleNamespace(status="needs_data_review", failure_message="Missing total")
    _clear_stale_vendor_flag(other)
    assert other.failure_message == "Missing total"


def test_create_alias_normalizes_and_persists_mapping():
    vendor = _vendor()
    canonical_query = MagicMock()
    canonical_query.filter.return_value.first.return_value = None
    aliases_query = MagicMock()
    aliases_query.filter.return_value.first.return_value = None
    db, teardown = _setup()
    db.get.return_value = vendor
    db.query.side_effect = [canonical_query, aliases_query]

    def refresh(alias):
        alias.id = uuid.uuid4()
        alias.created_at = datetime(2026, 10, 19, tzinfo=UTC)
        alias.updated_at = alias.created_at

    db.refresh.side_effect = refresh
    try:
        response = client.post(
            f"/api/vendors/{vendor.id}/aliases",
            json={"alias": "  ACME \t WHOLESALE "},
        )
    finally:
        teardown()

    assert response.status_code == 201, response.json()
    assert response.json()["alias_norm"] == "acme wholesale"
    assert response.json()["vendor_id"] == str(vendor.id)
    assert response.json()["is_active"] is True
    db.commit.assert_called_once()
    assert isinstance(db.add.call_args.args[0], VendorAlias)


def test_create_alias_rejects_duplicate_alias():
    vendor = _vendor(name="Canonical Vendor")
    canonical_query = MagicMock()
    canonical_query.filter.return_value.first.return_value = None
    aliases_query = MagicMock()
    aliases_query.filter.return_value.first.return_value = _alias(vendor.id)
    db, teardown = _setup()
    db.get.return_value = vendor
    db.query.side_effect = [canonical_query, aliases_query]

    try:
        response = client.post(
            f"/api/vendors/{vendor.id}/aliases",
            json={"alias": "ALTERNATE VENDOR"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_create_alias_rejects_another_vendors_canonical_name():
    vendor = _vendor()
    other_vendor = _vendor(name="Alias Name", vendor_id=uuid.uuid4())
    canonical_query = MagicMock()
    canonical_query.filter.return_value.first.return_value = other_vendor
    db, teardown = _setup()
    db.get.return_value = vendor
    db.query.return_value = canonical_query

    try:
        response = client.post(
            f"/api/vendors/{vendor.id}/aliases",
            json={"alias": "Alias Name"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    assert "canonical name" in response.json()["detail"]
    db.add.assert_not_called()


def test_alias_list_includes_active_state():
    vendor = _vendor()
    aliases = [_alias(vendor.id), _alias(vendor.id, is_active=False)]
    query = MagicMock()
    query.filter.return_value.order_by.return_value.all.return_value = aliases
    db, teardown = _setup()
    db.get.return_value = vendor
    db.query.return_value = query

    try:
        response = client.get(f"/api/vendors/{vendor.id}/aliases")
    finally:
        teardown()

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert {item["is_active"] for item in response.json()} == {True, False}


def test_alias_update_changes_only_supplied_fields():
    vendor = _vendor()
    alias = _alias(vendor.id)
    alias_query = MagicMock()
    alias_query.filter.return_value.first.return_value = None
    alias_query.filter.return_value.filter.return_value.first.return_value = None
    db, teardown = _setup()
    db.get.side_effect = lambda model, _id: alias if model is VendorAlias else vendor
    db.query.return_value = alias_query

    try:
        response = client.patch(
            f"/api/vendors/{vendor.id}/aliases/{alias.id}",
            json={"is_active": False},
        )
    finally:
        teardown()

    assert response.status_code == 200, response.json()
    assert response.json()["alias_norm"] == "alternate vendor"
    assert response.json()["vendor_id"] == str(vendor.id)
    assert response.json()["is_active"] is False
    db.commit.assert_called_once()


def test_alias_deactivation_is_soft_and_persistent():
    vendor = _vendor()
    alias = _alias(vendor.id)
    db, teardown = _setup()
    db.get.return_value = alias

    try:
        response = client.delete(f"/api/vendors/{vendor.id}/aliases/{alias.id}")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert alias.is_active is False
    db.delete.assert_not_called()
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        ("get", "/api/vendors/{vendor_id}/aliases", None),
        (
            "post",
            "/api/vendors/{vendor_id}/aliases",
            {"alias": "Alternate Name"},
        ),
        (
            "patch",
            "/api/vendors/{vendor_id}/aliases/{alias_id}",
            {"is_active": False},
        ),
        (
            "delete",
            "/api/vendors/{vendor_id}/aliases/{alias_id}",
            None,
        ),
    ),
)
def test_alias_routes_require_authentication(method, path, body):
    vendor_id = uuid.uuid4()
    alias_id = uuid.uuid4()
    path = path.format(vendor_id=vendor_id, alias_id=alias_id)
    db, teardown = _setup()
    app.dependency_overrides.pop(get_current_user, None)

    try:
        request = getattr(client, method)
        response = request(path) if body is None else request(path, json=body)
    finally:
        teardown()

    assert response.status_code == 401
    db.get.assert_not_called()
    db.query.assert_not_called()


def test_alias_list_returns_not_found_for_unknown_vendor():
    db, teardown = _setup()
    db.get.return_value = None

    try:
        response = client.get(f"/api/vendors/{uuid.uuid4()}/aliases")
    finally:
        teardown()

    assert response.status_code == 404
    db.query.assert_not_called()
