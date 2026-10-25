import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db

client = TestClient(app)


def _setup_dashboard(rows=None, total=0):
    owner_id = uuid.uuid4()
    count_query = MagicMock()
    count_query.select_from.return_value = count_query
    count_query.outerjoin.return_value = count_query
    count_query.filter.return_value = count_query
    count_query.scalar.return_value = total

    invoices_query = MagicMock()
    invoices_query.outerjoin.return_value = invoices_query
    invoices_query.filter.return_value = invoices_query
    invoices_query.order_by.return_value = invoices_query
    invoices_query.offset.return_value = invoices_query
    invoices_query.limit.return_value = invoices_query
    invoices_query.all.return_value = rows or []

    db = MagicMock()
    db.query.side_effect = [count_query, invoices_query]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(owner_id),
        "type": "access",
    }

    def teardown():
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return db, count_query, invoices_query, teardown


def _invoice(**overrides):
    values = {
        "id": uuid.uuid4(),
        "source_document_id": uuid.uuid4(),
        "invoice_no": "INV-101",
        "vendor_name_raw": "ABC Supplies Ltd",
        "invoice_date": date(2026, 10, 1),
        "due_date": date(2026, 10, 31),
        "total_amount": Decimal("125.50"),
        "currency": "USD",
        "match_status": "pending",
        "final_status": None,
        "workflow_status": "received",
        "status": "processing",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_invoice_list_is_paginated_and_returns_dashboard_fields():
    invoice = _invoice()
    db, _, query, teardown = _setup_dashboard(
        [(invoice, "ABC Supplies", "extracted")],
        total=41,
    )

    try:
        response = client.get(
            "/api/invoices",
            params={"page": 3, "page_size": 10},
        )
    finally:
        teardown()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 41
    assert body["page"] == 3
    assert body["page_size"] == 10
    assert body["items"] == [
        {
            "invoice_id": str(invoice.id),
            "source_document_id": str(invoice.source_document_id),
            "received_at": None,
            "processing_status": "extracted",
            "source": None,
            "email_sender": None,
            "email_subject": None,
            "invoice_no": "INV-101",
            "vendor_name": "ABC Supplies",
            "invoice_date": "2026-10-01",
            "due_date": "2026-10-31",
            "total_amount": "125.50",
            "currency": "USD",
            "match_status": "pending",
            "final_status": None,
            "workflow_status": "received",
            "gl_code": None,
            "confidence": None,
            "confidence_level": "low",
                "overall_confidence_percent": 0,
            "can_resubmit": False,
            "status": "processing",
        }
    ]
    query.offset.assert_called_once_with(20)
    query.limit.assert_called_once_with(10)
    assert db.query.call_count == 2


def test_invoice_list_can_filter_by_purchase_order():
    po_id = uuid.uuid4()
    _, _, query, teardown = _setup_dashboard(total=0)
    try:
        response = client.get("/api/invoices", params={"po_id": str(po_id)})
    finally:
        teardown()

    assert response.status_code == 200
    assert any("invoices.po_id" in str(item) for item in query.filter.call_args.args)


def test_invoice_list_accepts_verification_filters_and_sorting():
    invoice = _invoice(gl_code="6100", field_confidence={"total": {"confidence": 92}})
    _db, _count_query, query, teardown = _setup_dashboard(
        [(invoice, "ABC Supplies", "extracted")], total=1
    )
    try:
        response = client.get(
            "/api/invoices",
            params={
                "workflow_status": "verifying",
                "bucket": "invoice",
                "amount_min": "10",
                "amount_max": "200",
                "gl_code": "6100",
                "confidence_min": "85",
                "sort_by": "days_overdue",
            },
        )
    finally:
        teardown()
    assert response.status_code == 200
    assert query.filter.called
    assert query.order_by.called


def test_invoice_sort_direction_is_applied_and_invalid_value_is_rejected():
    _db, _count_query, query, teardown = _setup_dashboard([], total=0)
    try:
        response = client.get(
            "/api/invoices", params={"sort_by": "amount", "sort_dir": "asc"}
        )
        invalid = client.get("/api/invoices", params={"sort_dir": "sideways"})
    finally:
        teardown()
    assert response.status_code == 200
    assert query.order_by.called
    ordering = str(
        query.order_by.call_args.args[0].compile(dialect=postgresql.dialect())
    )
    assert "total_amount ASC" in ordering

    assert invalid.status_code == 422


def test_overdue_filter_excludes_approved_and_rejected_invoices():

    invoice = _invoice()
    _, count_query, _, teardown = _setup_dashboard(
        [(invoice, "ABC Supplies", "extracted")], total=1
    )
    try:
        response = client.get("/api/invoices?overdue=true")
    finally:
        teardown()
    assert response.status_code == 200
    expressions = [
        item.compile(dialect=postgresql.dialect())
        for item in count_query.filter.call_args.args
    ]
    sql = " ".join(str(expression) for expression in expressions)
    assert "invoices.due_date <" in sql
    assert "invoices.final_status NOT IN" in sql
    assert "invoices.final_status IS NULL" in sql


def test_invoice_detail_flags_low_confidence_extraction_fields():

    invoice = _invoice(
        vendor_id=None, subtotal=None, tax_amount=None, po_id=None, po_number_raw=None
    )
    document = SimpleNamespace(
        extracted_json={"field_confidence": {"invoice_no": 84.9, "total_amount": 95}}
    )
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value = query
    query.first.return_value = (invoice, document)
    db = MagicMock()
    db.query.return_value = query
    query.scalar.return_value = 0
    owner_id = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    try:
        response = client.get(f"/api/invoices/{invoice.id}")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200
    assert response.json()["low_confidence_fields"] == ["invoice_no"]
    assert response.json()["confidence_level"] == "low"
    assert response.json()["supporting_documents_count"] == 0


def test_invoice_list_applies_filters_and_search():
    vendor_id = uuid.uuid4()
    _, _, query, teardown = _setup_dashboard()

    try:
        response = client.get(
            "/api/invoices",
            params={
                "match_status": "match_issues",
                "final_status": "on_hold",
                "vendor_id": str(vendor_id),
                "invoice_date_from": "2026-09-01",
                "invoice_date_to": "2026-10-01",
                "due_date_from": "2026-10-15",
                "due_date_to": "2026-11-01",
                "search": "INV-10",
            },
        )
    finally:
        teardown()

    assert response.status_code == 200
    expressions = query.filter.call_args.args
    compiled = [
        expression.compile(dialect=postgresql.dialect()) for expression in expressions
    ]
    sql = " ".join(str(statement) for statement in compiled)
    params = {
        item
        for statement in compiled
        for value in statement.params.values()
        for item in (value if isinstance(value, (list, tuple)) else [value])
    }
    assert "invoices.match_status" in sql
    assert "invoices.final_status" in sql
    assert "invoices.vendor_id" in sql
    assert "invoices.invoice_date" in sql
    assert "invoices.due_date" in sql
    assert "invoices.invoice_no" in sql
    assert "invoices.vendor_name_raw" in sql
    assert "vendors.name" in sql
    assert "documents.created_by" in sql
    assert "invoices.owner_user_id" in sql
    assert "%INV-10%" in params


def test_review_queue_includes_only_undecided_invoices():
    _, _, query, teardown = _setup_dashboard()

    try:
        response = client.get("/api/invoices/review-queue")
    finally:
        teardown()

    assert response.status_code == 200
    expressions = query.filter.call_args.args
    compiled_sql = [
        str(expression.compile(dialect=postgresql.dialect()))
        for expression in expressions
    ]
    assert any(
        "invoices.final_status IS NULL" in statement for statement in compiled_sql
    )


def test_invoice_list_returns_empty_page():
    _, _, query, teardown = _setup_dashboard(rows=[], total=0)

    try:
        response = client.get("/api/invoices")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    query.all.assert_called_once()


def test_recommendation_hides_invoice_owned_by_another_user():
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value.first.return_value = None
    db = MagicMock()
    db.query.return_value = query
    owner_id = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(owner_id),
        "type": "access",
    }

    try:
        response = client.get(f"/api/invoices/{uuid.uuid4()}/recommendation")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404
    assert query.filter.call_count == 1
    owner_scope = query.filter.call_args.args[1].compile(dialect=postgresql.dialect())
    assert "documents.created_by" in str(owner_scope)
    assert "invoices.owner_user_id" in str(owner_scope)


def test_recommendation_api_returns_server_error_when_database_is_unavailable():
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.side_effect = RuntimeError("database unavailable")
    db = MagicMock()
    db.query.return_value = query
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid.uuid4()),
        "type": "access",
    }
    non_raising_client = TestClient(app, raise_server_exceptions=False)

    try:
        response = non_raising_client.get(
            f"/api/invoices/{uuid.uuid4()}/recommendation"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 500


def test_invoice_list_rejects_invalid_pagination():
    db, _, _, teardown = _setup_dashboard()

    try:
        response = client.get("/api/invoices?page=0&page_size=101")
    finally:
        teardown()

    assert response.status_code == 422
    db.query.assert_not_called()


def test_invoice_list_rejects_invalid_filters_and_date_ranges():
    invalid_requests = (
        "/api/invoices?match_status=unknown",
        "/api/invoices?final_status=pending",
        "/api/invoices?vendor_id=not-a-uuid",
        "/api/invoices?invoice_date_from=2026-10-10&invoice_date_to=2026-10-01",
        "/api/invoices?due_date=2026-10-01&due_date_from=2026-09-01",
        "/api/invoices?search=%20%20",
    )

    for url in invalid_requests:
        db, _, _, teardown = _setup_dashboard()
        try:
            response = client.get(url)
        finally:
            teardown()
        assert response.status_code == 422, url
        db.query.assert_not_called()


def test_invoice_list_requires_authentication():
    db, _, _, teardown = _setup_dashboard()
    app.dependency_overrides.pop(get_current_user, None)

    try:
        response = client.get("/api/invoices")
    finally:
        teardown()

    assert response.status_code == 401
    db.query.assert_not_called()
