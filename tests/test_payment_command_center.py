import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import command_center_routers
from src.db.session_db import get_db
from src.services import follow_up_services


def test_overdue_bucket_reports_timezone_date_difference(monkeypatch):
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        invoice_no="INV-E2E-1",
        vendor_id=None,
        vendor_name_raw="Example Vendor",
        workflow_status="overdue",
        final_status="approved",
        due_date=date(2026, 10, 20),
        total_amount=Decimal("100.00"),
        currency="INR",
        owner_user_id=None,
        is_current=True,
    )
    monkeypatch.setattr(
        command_center_routers, "_app_local_today", lambda: date(2026, 10, 22)
    )
    db = MagicMock()
    total_query, items_query, issues_query = MagicMock(), MagicMock(), MagicMock()
    db.query.side_effect = [total_query, items_query, issues_query]
    total_query.outerjoin.return_value.filter.return_value.scalar.return_value = 1
    item_filters = items_query.outerjoin.return_value.outerjoin.return_value
    item_filters = item_filters.filter.return_value.order_by.return_value
    items_page = item_filters.offset.return_value.limit.return_value
    items_page.all.return_value = [(invoice, None, "processed")]
    issues_query.filter.return_value.all.return_value = []

    result = command_center_routers.command_center_bills(
        bucket="overdue",
        page=1,
        page_size=25,
        sort_by="date_received",
        db=db,
        _user={"sub": str(uuid.uuid4())},
    )
    row = result["items"][0]
    assert row["days_overdue"] == 2
    assert row["alert"] is True
    assert row["reason"] == "unassigned"
    assert "pay" in row["actions"]


def test_command_center_sort_direction_and_invalid_value(monkeypatch):
    user_id = uuid.uuid4()
    db = MagicMock()

    def prepare_query_pair():
        total_query, items_query = MagicMock(), MagicMock()
        db.query.side_effect = [total_query, items_query]
        total_query.outerjoin.return_value.filter.return_value.scalar.return_value = 0
        item_query = items_query.outerjoin.return_value.outerjoin.return_value
        (
            item_query.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value
        ) = []
        return item_query

    asc_query = prepare_query_pair()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id)}
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        valid = client.get("/api/command-center/bills", params={"sort_dir": "asc"})
        asc_order = str(
            asc_query.filter.return_value.order_by.call_args.args[0].compile(
                dialect=postgresql.dialect()
            )
        )
        desc_query = prepare_query_pair()
        descending = client.get(
            "/api/command-center/bills", params={"sort_dir": "desc"}
        )
        invalid = client.get(
            "/api/command-center/bills", params={"sort_dir": "sideways"}
        )
        assert valid.status_code == 200
        assert descending.status_code == 200
        assert invalid.status_code == 422
        desc_order = str(
            desc_query.filter.return_value.order_by.call_args.args[0].compile(
                dialect=postgresql.dialect()
            )
        )
        assert "invoices.created_at ASC" in asc_order
        assert "invoices.created_at DESC" in desc_order
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_bucket_conditions_cover_unpaid_and_status_rules():
    today = date(2026, 10, 22)
    compiled = {
        bucket: str(
            command_center_routers._bucket_filter(bucket, today).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        for bucket in ("ready_to_pay", "overdue", "rejected", "needs_attention")
    }
    assert "status = 'ready_to_pay'" in compiled["ready_to_pay"]
    assert "paid_at IS NULL" in compiled["ready_to_pay"]
    assert "due_date < '2026-10-22'" in compiled["overdue"]
    assert "final_status != 'rejected'" in compiled["overdue"]
    assert "workflow_status = 'rejected'" in compiled["rejected"]
    assert "invoice_issues" in compiled["needs_attention"]


def test_app_local_today_uses_configured_timezone(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, 20, 30, tzinfo=UTC).astimezone(tz)

    from src.api.rest.routers import invoice_routers

    monkeypatch.setenv("APP_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setattr(invoice_routers, "datetime", FixedDateTime)
    assert invoice_routers._app_local_today() == date(2026, 10, 22)


def test_mark_overdue_transitions_and_creates_a_followup(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="approved",
        final_status="approved",
        is_current=True,
        due_date=date(2026, 10, 20),
        owner_user_id=None,
        invoice_no="INV-OVERDUE",
        source_document_id=None,
    )
    query = MagicMock()
    query.filter.return_value.with_for_update.return_value.all.return_value = [invoice]
    db = MagicMock()
    db.query.return_value = query
    monkeypatch.setattr(follow_up_services, "_queue_internal", lambda *args: [])
    monkeypatch.setattr(follow_up_services, "_dispatch_notifications", lambda ids: None)

    changed = follow_up_services.mark_overdue(db, datetime(2026, 10, 22, tzinfo=UTC))
    assert changed == 1
    assert invoice.workflow_status == "overdue"
    followup = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], follow_up_services.FollowUp)
    )
    assert followup.kind == "overdue"
    assert followup.next_due_at == datetime(2026, 10, 22, tzinfo=UTC)
    db.commit.assert_called_once()


def test_follow_up_stops_when_invoice_is_paid_or_reassigned():
    invoice = SimpleNamespace(
        workflow_status="paid",
        owner_user_id=uuid.uuid4(),
        due_date=None,
    )
    db = MagicMock()
    db.get.return_value = invoice
    row = SimpleNamespace(
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        kind="approval_pending",
        recipient="old-owner@example.invalid",
    )
    assert follow_up_services._stopped_reason(db, row) == "invoice paid"

    invoice.workflow_status = "match_approved"
    db.get.side_effect = [
        invoice,
        SimpleNamespace(email="new-owner@example.invalid", is_active=True),
    ]
    assert follow_up_services._stopped_reason(db, row) == "invoice reassigned"


def test_celery_schedules_overdue_and_followups_on_email_queue():
    from src.worker.celery_app import celery_app

    assert celery_app.conf.task_routes["ap.mark_overdue"]["queue"] == "email"
    assert celery_app.conf.task_routes["ap.run_follow_ups"]["queue"] == "email"
    schedule = celery_app.conf.beat_schedule
    assert (
        schedule["mark-overdue-invoices-hourly"]["schedule"].run_every.total_seconds()
        == 3600
    )
    assert (
        schedule["run-follow-ups-every-15-minutes"][
            "schedule"
        ].run_every.total_seconds()
        == 900
    )


def _due_followup_db(row, entity):
    query = MagicMock()
    locked = (
        query.filter.return_value.order_by.return_value.with_for_update.return_value
    )
    locked.all.return_value = [row]
    db = MagicMock()
    db.query.return_value = query
    db.get.return_value = entity
    return db


def test_follow_up_cap_stops_after_max_reminders(monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=UTC)
    invoice = SimpleNamespace(workflow_status="verifying", owner_user_id=None)
    row = SimpleNamespace(
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        kind="verify_pending",
        recipient=None,
        attempts=2,
        next_due_at=now,
        stopped_at=None,
        last_sent_at=None,
    )
    db = _due_followup_db(row, invoice)
    monkeypatch.setenv("MAX_REMINDERS", "3")
    monkeypatch.setattr(follow_up_services, "schedule_follow_ups", lambda *args: 0)
    monkeypatch.setattr(follow_up_services, "_stopped_reason", lambda *args: None)
    monkeypatch.setattr(follow_up_services, "_queue_internal", lambda *args: [])
    monkeypatch.setattr(follow_up_services, "_audit", lambda *args: None)
    monkeypatch.setattr(follow_up_services, "_dispatch_notifications", lambda ids: None)

    result = follow_up_services.run_follow_ups(db, now)
    assert result["sent"] == 1
    assert row.attempts == 3
    assert row.stopped_at == now
    assert row.stop_reason == "maximum reminder count reached"


def test_vendor_reply_reminder_creates_draft_and_later_escalates(monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=UTC)
    issue = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        status="waiting_vendor",
        created_at=now - follow_up_services.timedelta(days=4),
        description="Price variance",
    )
    row = SimpleNamespace(
        entity_type="issue",
        entity_id=issue.id,
        kind="issue_waiting_vendor",
        recipient="owner@example.invalid",
        attempts=0,
        next_due_at=now,
        stopped_at=None,
        last_sent_at=None,
    )
    db = _due_followup_db(row, issue)
    monkeypatch.setattr(follow_up_services, "schedule_follow_ups", lambda *args: 0)
    monkeypatch.setattr(follow_up_services, "_stopped_reason", lambda *args: None)
    monkeypatch.setattr(
        follow_up_services,
        "_vendor_wait_started_at",
        lambda db, value: value.created_at,
    )
    draft = MagicMock()
    draft_call = MagicMock(return_value=draft)
    monkeypatch.setattr(follow_up_services, "_create_vendor_draft", draft_call)
    monkeypatch.setattr(follow_up_services, "_audit", lambda *args: None)
    dispatch = MagicMock()
    monkeypatch.setattr(follow_up_services, "_dispatch_notifications", dispatch)

    follow_up_services.run_follow_ups(db, now)
    draft_call.assert_called_once()
    dispatch.assert_called_once_with([])
    assert row.next_due_at == issue.created_at + follow_up_services.timedelta(days=7)

    issue.created_at = now - follow_up_services.timedelta(days=8)
    row.next_due_at = now
    row.stopped_at = None
    row.attempts = 0
    db = _due_followup_db(row, issue)
    monkeypatch.setattr(follow_up_services, "add_issue_event", lambda *args: None)
    monkeypatch.setattr(
        follow_up_services, "_queue_internal", lambda *args: [uuid.uuid4()]
    )
    dispatch.reset_mock()
    follow_up_services.run_follow_ups(db, now)
    assert issue.status == "escalated"
    assert row.stop_reason == "vendor reply escalation sent"
    dispatch.assert_called_once()


def test_schedule_followups_uses_state_and_vendor_wait_start(monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=UTC)
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        is_current=True,
        created_at=now - follow_up_services.timedelta(days=20),
        owner_user_id=None,
        workflow_status="verifying",
        due_date=None,
    )
    invoice_query, issue_query = MagicMock(), MagicMock()
    invoice_query.filter.return_value.all.return_value = [invoice]
    issue_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [invoice_query, issue_query]
    scheduled = []

    def capture(_db, entity_type, entity_id, kind, recipient, due):
        scheduled.append((entity_type, entity_id, kind, recipient, due))
        return SimpleNamespace()

    monkeypatch.setattr(follow_up_services, "_ensure_followup", capture)
    created = follow_up_services.schedule_follow_ups(db, now)
    verify = next(item for item in scheduled if item[2] == "verify_pending")
    assert verify[4] == invoice.created_at + follow_up_services.timedelta(days=1)
    assert created == 1
