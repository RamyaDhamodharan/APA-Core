import smtplib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.models.email_draft_models import EmailDraft
from src.services import email_draft_services as drafts


@pytest.mark.parametrize("kind", sorted(drafts.SUPPORTED_KINDS))
def test_templates_keep_only_structured_facts_and_exact_figures(kind):
    facts = {
        "vendor": "Test Vendor",
        "invoice": "INV-101",
        "PO": "PO-22",
        "expected": "100.00",
        "billed": "102.00",
    }
    subject, body = drafts._template(kind, facts)
    assert subject
    assert all(str(value) in body for value in facts.values())
    assert "103.00" not in body
    assert "invented" not in body.casefold()


def test_draft_generation_blocks_recipient_not_on_allowlist(monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "test@allowed.invalid")
    vendor = SimpleNamespace(name="Fake Vendor", contact_email="vendor@example.invalid")
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        vendor_name_raw="Fake Vendor",
        invoice_no="INV-1",
        po_number_raw="PO-2",
        po_id=None,
        total_amount=12,
        currency="USD",
        tax_amount=None,
        gl_code=None,
        source_document_id=None,
    )

    class DB:
        rows = []

        def get(self, _model, _id):
            return vendor

        def query(self, _model):
            raise AssertionError("no line query expected without a PO")

        def add(self, row):
            self.rows.append(row)

        def flush(self):
            pass

    db = DB()
    draft = drafts.create_invoice_draft(db, invoice, "price_variance")
    assert draft.status == "draft"
    assert draft.blocked_by_allowlist is True
    assert draft.to_addresses == ["vendor@example.invalid"]
    assert "12.00" in draft.body
    assert draft.generated_by == "template"


class FakeSMTP:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send_message(self, message):
        self.calls += 1
        assert "[AP-" in message["Subject"]
        assert message.get_body(preferencelist=("plain",)).get_content()
        if self.calls <= self.failures:
            raise smtplib.SMTPServerDisconnected("temporary unavailable")


def test_draft_send_retries_smtp_error_then_sends(monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "vendor@example.invalid")
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_PORT", "2525")
    smtp = FakeSMTP(failures=1)
    monkeypatch.setattr(drafts.smtplib, "SMTP", lambda *_args, **_kwargs: smtp)
    delays = []
    draft = EmailDraft(
        kind="clarification_request",
        to_addresses=["vendor@example.invalid"],
        cc_addresses=[],
        subject="Please clarify",
        body="Test only",
        status="draft",
        attempts=0,
        thread_key="[AP-ABCD1234]",
    )
    db = SimpleNamespace(rows=[])
    db.add = db.rows.append
    assert drafts.send_draft(db, draft, sleep=delays.append) is True
    assert smtp.calls == 2
    assert delays == [1]
    assert draft.status == "sent"
    assert draft.message_id
    with pytest.raises(ValueError, match="already been sent"):
        drafts.send_draft(db, draft, sleep=delays.append)


def test_draft_send_refuses_unallowlisted_recipient(monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "")
    draft = EmailDraft(
        kind="clarification_request",
        to_addresses=["vendor@example.invalid"],
        cc_addresses=[],
        subject="Please clarify",
        body="Test only",
        status="draft",
        attempts=0,
    )
    assert drafts.send_draft(SimpleNamespace(add=lambda _row: None), draft) is False
    assert draft.status == "draft"
    assert draft.blocked_by_allowlist is True
    assert "allowlist" in draft.last_error.casefold()


def test_permanent_smtp_error_is_not_retried(monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "vendor@example.invalid")

    class PermanentFailure:
        def __enter__(self):
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        drafts.smtplib, "SMTP", lambda *_args, **_kwargs: PermanentFailure()
    )
    delays = []
    draft = EmailDraft(
        kind="clarification_request",
        to_addresses=["vendor@example.invalid"],
        cc_addresses=[],
        subject="Please clarify",
        body="Test only",
        status="draft",
        attempts=0,
    )
    assert (
        drafts.send_draft(
            SimpleNamespace(add=lambda _row: None), draft, sleep=delays.append
        )
        is False
    )
    assert draft.status == "failed"
    assert draft.attempts == 1
    assert delays == []


def test_reply_matches_sent_draft_token_and_message_id():
    draft = SimpleNamespace(
        status="sent",
        thread_key="[AP-A1B2C3D4]",
        message_id="<outbound@example.invalid>",
    )

    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [draft]

    db = SimpleNamespace(query=lambda _model: Query())
    assert drafts.match_reply(db, "Re: [AP-A1B2C3D4] question", None) is draft
    assert (
        drafts.match_reply(db, "Re: follow up", "<outbound@example.invalid>") is draft
    )


def test_reply_event_is_linked_to_sent_draft_and_attachment_document():
    draft = SimpleNamespace(id=uuid.uuid4())
    db = SimpleNamespace(rows=[])
    db.add = db.rows.append
    document_id = uuid.uuid4()
    drafts.record_vendor_reply(
        db,
        draft,
        sender="vendor@example.invalid",
        subject="Re: invoice",
        message_id="<reply@example.invalid>",
        document_id=document_id,
    )
    event = db.rows[0]
    assert event.event_type == "vendor_replied"
    assert event.document_id == document_id
    assert event.message_id == "<reply@example.invalid>"


def test_edit_then_send_is_audited_and_second_send_conflicts(monkeypatch):
    from fastapi import HTTPException

    from src.api.rest.routers import email_draft_routers as router

    user_id = uuid.uuid4()
    draft = EmailDraft(
        id=uuid.uuid4(),
        kind="clarification_request",
        to_addresses=["vendor@example.invalid"],
        cc_addresses=[],
        subject="Old subject",
        body="Old body",
        status="draft",
        attempts=0,
    )

    class Query:
        def filter_by(self, **_kwargs):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return draft

    db = SimpleNamespace(rows=[], query=lambda _model: Query())
    db.add = db.rows.append
    db.commit = lambda: None
    db.refresh = lambda _row: None

    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "vendor@example.invalid")
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    smtp = FakeSMTP()
    monkeypatch.setattr(drafts.smtplib, "SMTP", lambda *_args, **_kwargs: smtp)

    edited = router.edit_email_draft(
        draft.id,
        router.EditDraft(subject="Updated subject", body="Updated body"),
        db,
        {"sub": str(user_id)},
    )
    assert edited["subject"] == "Updated subject"
    edited_event = next(
        row for row in db.rows if getattr(row, "event_type", None) == "edited"
    )
    assert edited_event.user_id == user_id
    assert edited_event.old_value == {"subject": "Old subject", "body": "Old body"}

    result = router.send_email_draft(draft.id, db, {"sub": str(user_id)})
    assert result["sent"] is True
    assert smtp.calls == 1
    sent_audit = next(
        row for row in db.rows if getattr(row, "action", None) == "draft_sent"
    )
    assert sent_audit.actor == str(user_id)
    assert sent_audit.created_by == user_id
    assert sent_audit.reason
    with pytest.raises(HTTPException) as exc:
        router.send_email_draft(draft.id, db, {"sub": str(user_id)})
    assert exc.value.status_code == 409


def test_invoice_timeline_contains_outbound_draft_events():
    from src.api.rest.routers.invoice_routers import invoice_timeline
    from src.models.email_draft_models import EmailDraftEvent

    invoice_id = uuid.uuid4()
    event = SimpleNamespace(
        id=uuid.uuid4(),
        draft_id=uuid.uuid4(),
        event_type="sent",
        old_value={"status": "draft"},
        new_value={"status": "sent"},
        user_id=uuid.uuid4(),
        created_at=datetime.now(UTC),
    )

    class Query:
        def __init__(self, rows, first=None):
            self.rows = rows
            self.first_row = first

        def filter(self, *_args):
            return self

        def filter_by(self, **_kwargs):
            return self

        def join(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def first(self):
            return self.first_row

        def all(self):
            return self.rows

    invoice = SimpleNamespace(source_document_id=None, po_id=None)
    rows = {
        EmailDraftEvent: [event],
    }
    db = SimpleNamespace(
        query=lambda model: Query(
            rows.get(model, []), first=invoice if model.__name__ == "Invoice" else None
        ),
    )
    timeline = invoice_timeline(invoice_id, db, {"sub": str(uuid.uuid4())})
    assert len(timeline) == 1
    assert timeline[0]["kind"] == "email"
    assert timeline[0]["action"] == "email_sent"
