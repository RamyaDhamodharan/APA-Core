import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.rest.routers import invoice_routers, issue_routers
from src.models.audit_models import AuditLog
from src.models.invoice_issue_models import InvoiceIssue, IssueEvent
from src.models.invoice_models import Invoice
from src.models.notification_models import Notification
from src.schemas.invoice_schemas import InvoiceReviewRequest
from src.services import email_draft_services
from src.services import invoice_issue_services as issues


def _invoice(**kwargs):
    defaults = {
        "id": uuid.uuid4(),
        "po_id": uuid.uuid4(),
        "vendor_id": uuid.uuid4(),
        "field_confidence": {},
        "owner_user_id": uuid.uuid4(),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_match_difference_creates_issue_with_exact_values_and_audit(monkeypatch):
    monkeypatch.setattr(issues, "_assignment", lambda *_args, **_kwargs: None)
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = []
    db = MagicMock()
    db.query.return_value = query
    invoice = _invoice()

    created = issues.sync_invoice_issues(
        db,
        invoice,
        [
            {
                "name": "invoice_vs_po_unit_price",
                "outcome": "mismatch",
                "severity": "error",
                "expected": "100.00",
                "actual": "105.00",
                "reason": "Billed price exceeds PO price",
            }
        ],
        uuid.uuid4(),
    )

    issue = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], InvoiceIssue)
    )
    assert created == ["price_variance"]
    assert issue.expected_value == {"value": "100.00"}
    assert issue.actual_value == {"value": "105.00"}
    assert issue.status == "open"
    assert any(
        getattr(call.args[0], "action", "").endswith("created")
        for call in db.add.call_args_list
    )


def test_rematch_updates_active_issue_without_duplicate(monkeypatch):
    monkeypatch.setattr(issues, "_assignment", lambda *_args, **_kwargs: None)
    existing = SimpleNamespace(
        issue_type="tax_mismatch",
        status="open",
        description="old",
        expected_value=None,
        actual_value=None,
        severity="warning",
    )
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = [existing]
    db = MagicMock()
    db.query.return_value = query

    issues.sync_invoice_issues(
        db,
        _invoice(),
        [
            {
                "name": "invoice_vs_po_tax",
                "outcome": "mismatch",
                "expected": 10,
                "actual": 12,
                "severity": "error",
                "reason": "Tax differs",
            }
        ],
    )

    assert existing.description == "Tax differs"
    assert existing.expected_value == {"value": 10}
    assert not any(
        isinstance(call.args[0], InvoiceIssue) for call in db.add.call_args_list
    )


def test_rematch_resolves_issue_when_difference_disappears():
    issue = SimpleNamespace(
        id=uuid.uuid4(),
        issue_type="price_variance",
        status="in_progress",
        resolved_at=None,
        resolution_note=None,
    )
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = [issue]
    db = MagicMock()
    db.query.return_value = query

    issues.sync_invoice_issues(db, _invoice(), [])

    assert issue.status == "resolved"
    assert issue.resolved_at.tzinfo == UTC
    assert "Automatically resolved" in issue.resolution_note


def test_assignment_manual_mode_leaves_issue_unassigned(monkeypatch):
    monkeypatch.setenv("ASSIGNMENT_STRATEGY", "manual")
    assert issues._assignment(MagicMock()) is None


def test_resolve_and_dismiss_write_status_and_audit(monkeypatch):
    for final_status, event, endpoint in (
        ("resolved", "resolved", issue_routers.resolve_issue),
        ("dismissed", "dismissed", issue_routers.dismiss_issue),
    ):
        issue = SimpleNamespace(
            id=uuid.uuid4(),
            invoice_id=uuid.uuid4(),
            issue_type="price_variance",
            severity="warning",
            status="open",
            expected_value=None,
            actual_value=None,
            owner_user_id=None,
            description="Mismatch",
            due_at=None,
            created_at=datetime.now(UTC),
            resolved_at=None,
            resolution_note=None,
        )
        invoice = SimpleNamespace(
            workflow_status="match_issues",
            invoice_no="I-1",
            vendor_id=None,
            id=issue.invoice_id,
        )
        db = MagicMock()
        db.get.return_value = invoice
        db.query.return_value.filter.return_value.count.return_value = 0
        monkeypatch.setattr(issue_routers, "_get", lambda *_args, issue=issue: issue)
        monkeypatch.setattr(
            issue_routers, "transition_invoice", lambda *_a, **_kw: None
        )

        result = endpoint(
            issue.id,
            issue_routers.NoteRequest(note="Reviewed evidence"),
            db,
            {"sub": str(uuid.uuid4()), "role": "associate"},
        )

        assert result["status"] == final_status
        assert any(isinstance(call.args[0], AuditLog) for call in db.add.call_args_list)
        assert any(
            isinstance(call.args[0], IssueEvent) and call.args[0].event_type == event
            for call in db.add.call_args_list
        )
        db.reset_mock()


def test_issue_override_does_not_bypass_saved_match_rules(monkeypatch):
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        match_status="match_issues",
        workflow_status="match_issues",
        final_status=None,
        final_by=None,
        final_at=None,
    )
    user_id = uuid.uuid4()
    db = MagicMock()
    invoice_query = db.query.return_value
    (
        invoice_query.outerjoin.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = invoice
    issue_query = MagicMock()
    issue_query.filter.return_value.count.return_value = 1
    db.query.side_effect = [invoice_query, issue_query]

    with pytest.raises(HTTPException) as blocked:
        invoice_routers.review_invoice(
            invoice.id,
            InvoiceReviewRequest(decision="approved"),
            db,
            {"sub": str(user_id), "role": "associate"},
        )
    assert blocked.value.status_code == 409

    db.reset_mock()
    invoice.final_status = None
    db.query.side_effect = [invoice_query, issue_query]
    with pytest.raises(HTTPException) as blocked:
        invoice_routers.review_invoice(
            invoice.id,
            InvoiceReviewRequest(
                decision="approved", override_reason="Business owner accepted risk"
            ),
            db,
            {"sub": str(user_id), "role": "associate"},
        )
    assert blocked.value.status_code == 409


def test_escalation_notifies_allowlisted_associates(monkeypatch):
    issue = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        issue_type="tax_mismatch",
        severity="warning",
        status="in_progress",
        expected_value=None,
        actual_value=None,
        owner_user_id=None,
        description="Tax differs",
        due_at=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
        resolution_note=None,
    )
    user_id = uuid.uuid4()
    associate = SimpleNamespace(id=uuid.uuid4(), email="reviewer@example.test")
    invoice = SimpleNamespace(
        source_document_id=uuid.uuid4(),
        invoice_no="T-1",
        vendor_id=None,
        id=issue.invoice_id,
    )
    db = MagicMock()
    db.get.side_effect = lambda model, _key: invoice if model is Invoice else None
    associate_query = MagicMock()
    associate_query.filter.return_value.all.return_value = [associate]
    db.query.return_value = associate_query
    monkeypatch.setattr(issue_routers, "_get", lambda *_args: issue)
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "reviewer@example.test")
    monkeypatch.delenv("ESCALATION_EMAIL", raising=False)

    result = issue_routers.escalate_issue(
        issue.id,
        issue_routers.EscalateRequest(reason="Needs manager review"),
        db,
        {"sub": str(user_id), "role": "associate"},
    )

    assert result["status"] == "escalated"
    notification = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Notification)
    )
    assert notification.recipient_email == associate.email


def test_vendor_reply_moves_waiting_issue_to_in_progress(monkeypatch):
    issue = SimpleNamespace(
        id=uuid.uuid4(), status="waiting_vendor", owner_user_id=None
    )
    draft = SimpleNamespace(issue_id=issue.id, id=uuid.uuid4())
    db = MagicMock()
    db.get.return_value = issue
    monkeypatch.setattr(email_draft_services, "_event", lambda *_a, **_kw: None)
    events = []
    monkeypatch.setattr(
        "src.services.invoice_issue_services.add_issue_event",
        lambda _db, _issue, event, _uid, note: events.append((event, note)),
    )

    email_draft_services.record_vendor_reply(
        db, draft, sender="vendor@example.test", subject="Reply", message_id="<m1>"
    )

    assert issue.status == "in_progress"
    assert events[0][0] == "vendor_replied"


def test_rejected_invoice_resubmit_creates_new_version_and_keeps_history(monkeypatch):
    old = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="rejected",
        version_no=3,
        is_current=True,
        source_document_id=uuid.uuid4(),
        invoice_no="INV-X",
        vendor_id=uuid.uuid4(),
        vendor_name_raw="Fake Vendor",
        vendor_tax_id_raw=None,
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=100,
        tax_amount=10,
        total_amount=110,
        field_confidence={},
        gl_code=None,
        po_number_raw="PO-X",
        po_id=uuid.uuid4(),
        match_status="pending",
        final_status="rejected",
        owner_user_id=uuid.uuid4(),
    )
    old_query = MagicMock()
    old_query.filter.return_value.with_for_update.return_value.first.return_value = old
    rejection = SimpleNamespace(new_value={"reason": "Wrong invoice amount"})
    audit_query = MagicMock()
    audit_query.filter.return_value.order_by.return_value.first.return_value = rejection
    lines_query = MagicMock()
    lines_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [old_query, audit_query, lines_query]
    monkeypatch.setattr(
        invoice_routers, "find_duplicate_invoice", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        invoice_routers,
        "transition_invoice",
        lambda _db, invoice, status, **_kw: setattr(invoice, "workflow_status", status),
    )
    monkeypatch.setattr(
        invoice_routers, "run_match_for_invoice", lambda *_a, **_kw: None
    )

    result = invoice_routers.resubmit_invoice(
        old.id,
        {"total_amount": 108},
        db,
        {"sub": str(uuid.uuid4()), "role": "associate"},
    )

    new_invoice = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Invoice)
    )
    resubmit_audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
        and call.args[0].action == "invoice_resubmitted"
    )
    assert old.is_current is False
    assert new_invoice.is_current is True
    assert new_invoice.version_no == 4
    assert new_invoice.resubmitted_from_id == old.id
    assert new_invoice.total_amount == 108
    assert new_invoice.workflow_status == "matching"
    assert "Wrong invoice amount" in resubmit_audit.old_value["rejection_reason"]
    assert result["version_no"] == 4


def test_resubmit_rejects_real_duplicate_invoice_number_with_409():
    old_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    old = SimpleNamespace(
        id=old_id,
        workflow_status="rejected",
        invoice_no="INV-1",
        vendor_id=vendor_id,
    )
    duplicate = Invoice(
        id=uuid.uuid4(),
        invoice_no="INV/2",
        invoice_no_normalized="INV2",
        vendor_id=vendor_id,
        is_current=True,
    )
    old_query = MagicMock()
    old_query.filter.return_value.with_for_update.return_value.first.return_value = old
    rejection_query = MagicMock()
    rejection_query.filter.return_value.order_by.return_value.first.return_value = None
    duplicate_query = MagicMock()
    (
        duplicate_query.filter.return_value.with_for_update.return_value.first.return_value
    ) = duplicate
    db = MagicMock()
    db.query.side_effect = [old_query, rejection_query, duplicate_query]

    with pytest.raises(HTTPException) as blocked:
        invoice_routers.resubmit_invoice(
            old_id,
            {"invoice_no": "INV-2"},
            db,
            {"sub": str(uuid.uuid4()), "role": "associate"},
        )

    assert blocked.value.status_code == 409
    assert blocked.value.detail == (
        "An invoice with this vendor and invoice number already exists"
    )
