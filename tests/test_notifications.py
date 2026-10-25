import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.user_models import User

client = TestClient(app)


def _setup_alerts(rows=None, total=0):
    query = MagicMock()
    query.filter.return_value = query
    query.with_entities.return_value = query
    query.scalar.return_value = total
    query.order_by.return_value = query
    query.offset.return_value = query
    query.limit.return_value = query
    query.all.return_value = rows or []

    db = MagicMock()
    db.query.return_value = query
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid.uuid4()),
        "type": "access",
    }

    def teardown():
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return db, query, teardown


def _setup_notification_list(rows=None, total=0):
    user_id = uuid.uuid4()
    user_query = MagicMock()
    user_query.filter.return_value.scalar.return_value = "reviewer@example.com"

    count_query = MagicMock()
    count_query.filter.return_value = count_query
    count_query.scalar.return_value = total

    list_query = MagicMock()
    list_query.filter.return_value = list_query
    list_query.outerjoin.return_value = list_query
    list_query.order_by.return_value = list_query
    list_query.offset.return_value = list_query
    list_query.limit.return_value = list_query
    list_query.all.return_value = rows or []

    db = MagicMock()

    db.query.side_effect = [user_query, count_query, list_query]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(user_id),
        "type": "access",
    }

    def teardown():
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return user_id, db, count_query, list_query, teardown


def _invoice(match_status, final_status=None, is_current=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        invoice_no="INV-901",
        match_status=match_status,
        final_status=final_status,
        is_current=is_current,
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


def test_exception_alerts_cover_each_attention_exception():
    cases = (
        ("match_issues", "match_issues"),
        ("waiting_for_po", "waiting_for_po"),
        ("needs_vendor_review", "needs_vendor_review"),
        ("pending", "pending_review"),
    )

    for match_status, exception_type in cases:
        invoice = _invoice(match_status)
        _, _, teardown = _setup_alerts([invoice], total=1)
        try:
            response = client.get("/api/notifications/exception-alerts")
        finally:
            teardown()

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["invoice_id"] == str(invoice.id)
        assert item["invoice_no"] == "INV-901"
        assert item["exception_type"] == exception_type
        assert item["status"] == match_status
        assert item["final_status"] is None
        assert item["created_at"] == "2026-10-18T00:00:00Z"


def test_exception_alerts_prioritize_matching_exception_without_duplicate():
    invoice = _invoice("match_issues")
    _, _, teardown = _setup_alerts([invoice], total=1)

    try:
        response = client.get("/api/notifications/exception-alerts")
    finally:
        teardown()

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["exception_type"] == "match_issues"


def test_decided_invoices_are_not_classified_as_pending_review():
    from src.api.rest.routers.notification_routers import (
        _notification_exception_type,
    )

    for final_status in ("approved", "rejected", "on_hold"):
        invoice = _invoice("pending", final_status)
        assert _notification_exception_type(invoice) == "notification"


def test_exception_alerts_only_include_current_invoices():
    _, query, teardown = _setup_alerts([], total=0)

    try:
        response = client.get("/api/notifications/exception-alerts?page=2&page_size=5")
    finally:
        teardown()

    assert response.status_code == 200
    compiled = [
        expression.compile(dialect=postgresql.dialect())
        for expression in query.filter.call_args.args
    ]
    assert any("invoices.is_current" in str(expression) for expression in compiled)
    assert any(
        "invoices.final_status IS NULL" in str(expression) for expression in compiled
    )
    assert any("invoices.match_status IN" in str(expression) for expression in compiled)
    query.offset.assert_called_once_with(5)
    query.limit.assert_called_once_with(5)


def test_exception_alerts_returns_empty_results():
    _, _, teardown = _setup_alerts([], total=0)

    try:
        response = client.get("/api/notifications/exception-alerts")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_queued_notification_is_sent_to_allowlisted_mailbox(monkeypatch):
    from src.services.notification_services import send_notification

    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "test@example.com")
    notification = SimpleNamespace(
        recipient_email="test@example.com",
        subject="Review",
        body="Please review",
        status="queued",
        attempts=0,
        sent_at=None,
        last_error=None,
    )
    smtp = MagicMock()
    smtp_context = MagicMock()
    smtp_context.__enter__.return_value = smtp
    monkeypatch.setattr(
        "src.services.notification_services.smtplib.SMTP", lambda *a, **k: smtp_context
    )

    assert send_notification(notification) is True
    assert notification.status == "sent"
    assert notification.attempts == 1
    smtp.send_message.assert_called_once()


def test_failed_notification_can_be_retried(monkeypatch):
    from src.services.notification_services import send_notification

    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "test@example.com")
    notification = SimpleNamespace(
        recipient_email="test@example.com",
        subject="Review",
        body="Please review",
        status="queued",
        attempts=0,
        sent_at=None,
        last_error=None,
    )
    failing_context = MagicMock()
    failing_context.__enter__.side_effect = ConnectionError("SMTP unavailable")
    monkeypatch.setattr(
        "src.services.notification_services.smtplib.SMTP",
        lambda *a, **k: failing_context,
    )

    assert send_notification(notification) is False
    assert notification.status == "failed"
    assert notification.attempts == 1

    good_context = MagicMock()
    good_context.__enter__.return_value = MagicMock()
    monkeypatch.setattr(
        "src.services.notification_services.smtplib.SMTP", lambda *a, **k: good_context
    )
    assert send_notification(notification) is True
    assert notification.status == "sent"
    assert notification.attempts == 2


def test_notification_fallback_uses_background_tasks(monkeypatch):
    from fastapi import BackgroundTasks

    from src.worker.dispatch import dispatch_notification

    monkeypatch.setenv("USE_CELERY", "false")
    background_tasks = BackgroundTasks()
    notification_id = uuid.uuid4()
    mode = dispatch_notification(background_tasks, notification_id)
    assert mode == "background"
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].args == (notification_id,)


def test_exception_alerts_require_authentication():
    db, _, teardown = _setup_alerts()
    app.dependency_overrides.pop(get_current_user, None)

    try:
        response = client.get("/api/notifications/exception-alerts")
    finally:
        teardown()

    assert response.status_code == 401
    db.query.assert_not_called()


def test_notifications_returns_existing_notification_with_invoice_context():
    notification = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        status="queued",
        subject="Invoice requires review",
        body="Review invoice INV-901",
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )
    invoice = _invoice("needs_vendor_review")
    _, _, count_query, list_query, teardown = _setup_notification_list(
        [(notification, invoice)],
        total=1,
    )

    try:
        response = client.get("/api/notifications")
    finally:
        teardown()

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["notification_id"] == str(notification.id)
    assert item["document_id"] == str(notification.document_id)
    assert item["invoice_id"] == str(invoice.id)
    assert item["invoice_no"] == "INV-901"
    assert item["exception_type"] == "needs_vendor_review"
    assert item["status"] == "queued"
    assert item["created_at"] == "2026-10-18T00:00:00Z"
    assert "recipient_email" not in item
    assert "attempts" not in item
    assert "last_error" not in item

    notification_filter = count_query.filter.call_args.args[0]
    compiled_filter = notification_filter.compile(dialect=postgresql.dialect())
    assert "notifications.recipient_email" in str(compiled_filter)
    assert "reviewer@example.com" in compiled_filter.params.values()
    assert list_query.filter.call_count == 1


def test_notifications_returns_empty_results():
    _, _, _, list_query, teardown = _setup_notification_list([], total=0)

    try:
        response = client.get("/api/notifications?page=2&page_size=5")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    assert response.json()["page"] == 2
    assert response.json()["page_size"] == 5
    list_query.offset.assert_called_once_with(5)
    list_query.limit.assert_called_once_with(5)


def test_notifications_require_authentication():
    db, _, teardown = _setup_alerts()
    app.dependency_overrides.pop(get_current_user, None)

    try:
        response = client.get("/api/notifications")
    finally:
        teardown()

    assert response.status_code == 401
    db.query.assert_not_called()


def test_notifications_reject_unknown_authenticated_user():
    user_id = uuid.uuid4()
    user_query = MagicMock()
    user_query.filter.return_value.scalar.return_value = None
    db = MagicMock()
    db.query.return_value = user_query
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(user_id),
        "type": "access",
    }

    try:
        response = client.get("/api/notifications")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 401
    db.query.assert_called_once_with(User.email)
