import hashlib
import imaplib
import random
import struct
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MIMEMessage
from email.utils import format_datetime
from io import BytesIO
from unittest.mock import MagicMock
from zipfile import ZIP_DEFLATED, ZipFile

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.routers import email_polling_routers
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage as EmailMessageRecord
from src.models.email_models import EmailPollState
from src.models.notification_models import Notification
from src.models.user_models import User
from src.models.vendor_models import Vendor, VendorAlias
from src.services import document_ingest_services, email_polling_services

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes():
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr("word/document.xml", "<w:document/>")
    return output.getvalue()


class FakeQuery:
    def __init__(self, db, model):
        self.db = db
        self.model = model
        self.filters = {}

    def filter_by(self, **kwargs):
        self.filters.update(kwargs)
        return self

    def filter(self, *_):
        return self

    def order_by(self, *_):
        return self

    def offset(self, *_):
        return self

    def limit(self, *_):
        return self

    def with_for_update(self, **_kwargs):
        return self

    def first(self):
        return next(
            (
                row
                for row in self.db.rows
                if isinstance(row, self.model)
                and all(
                    getattr(row, key, None) == value
                    for key, value in self.filters.items()
                )
            ),
            None,
        )

    def all(self):
        return [
            row
            for row in self.db.rows
            if isinstance(row, self.model)
            and all(
                getattr(row, key, None) == value for key, value in self.filters.items()
            )
        ]

    def count(self):
        return len(self.all())


class FakeDB:
    def __init__(self):
        self.rows = []

    def query(self, model):
        return FakeQuery(self, model)

    def add(self, row):
        if isinstance(row, (Document, EmailMessageRecord, Notification)) and not row.id:
            row.id = uuid.uuid4()
        self.rows.append(row)

    def commit(self):
        pass

    def rollback(self):
        pass

    def refresh(self, _row):
        pass

    def get(self, model, row_id):
        return next(
            (row for row in self.rows if isinstance(row, model) and row.id == row_id),
            None,
        )


class FakeIMAP:
    def __init__(self, messages, uidvalidity=7, seen=(), internal_dates=None):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.seen = set(seen)
        self.internal_dates = internal_dates or {}
        self.logged_out = False
        self.selected_readonly = False
        self.fetch_queries = []
        self.search_queries = []

    def login(self, *_):
        return "OK", []

    def select(self, _folder, readonly=False):
        self.selected_readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, key):
        assert key == "UIDVALIDITY"
        return key, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "search":
            criteria = str(args[-1])
            self.search_queries.append(criteria)
            values = sorted(self.messages)
            if criteria.startswith("UID "):
                range_value = criteria.removeprefix("UID ")
                lower, _, upper = range_value.partition(":")
                high = int(upper) if upper and upper != "*" else max(values, default=0)
                values = [uid for uid in values if int(lower) <= uid <= high]
            elif criteria.startswith("SINCE "):
                pass
            elif criteria != "ALL":
                raise AssertionError(f"Unexpected IMAP search: {criteria}")
            return "OK", [b" ".join(str(uid).encode() for uid in values)]
        if command == "fetch":
            uid = int(args[0])
            query = str(args[1])
            self.fetch_queries.append((uid, query))
            if "HEADER.FIELDS" in query:
                original = email_polling_services.email.message_from_bytes(
                    self.messages[uid]
                )
                headers = MIMEMessage()
                headers["Message-ID"] = str(original.get("Message-ID", ""))
                return "OK", [
                    (b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)])", headers.as_bytes())
                ]
            assert "BODY.PEEK[]" in query
            received = self.internal_dates.get(uid, datetime.now(UTC))
            internal_date = received.strftime("%d-%b-%Y %H:%M:%S %z")
            header = f'1 (INTERNALDATE "{internal_date}") BODY[]'.encode()
            return "OK", [(header, self.messages[uid])]
        raise AssertionError(f"Unexpected IMAP command: {command}")

    def logout(self):
        self.logged_out = True


PDF = b"%PDF-1.7\n" + b"invoice contents " * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"image bytes " * 20
JPEG = b"\xff\xd8\xff" + b"image bytes " * 20


def _large_png() -> bytes:
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rng = random.Random(17)
    rows = b"".join(b"\0" + rng.randbytes(400) for _ in range(100))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _mail(
    message_id="<one@vendor.test>",
    attachments=(),
    *,
    sender="billing@test-mail.test",
    subject="Invoice",
    extra_headers=None,
    body="Please see attached.",
):
    message = MIMEMessage()
    message["From"] = sender
    message["To"] = "ap@test-mail.test"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(datetime.now(UTC))
    for key, value in (extra_headers or {}).items():
        message[key] = value
    message.set_content(body)
    for attachment in attachments:
        filename, data, maintype, subtype, *disposition = attachment
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
            **({"disposition": disposition[0]} if disposition else {}),
        )
    return message.as_bytes()


@pytest.fixture
def polling_setup(monkeypatch):
    db = FakeDB()
    uploads = []
    notifications = []
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER", "ap@test-mail.test")
    monkeypatch.setenv("IMAP_PASSWORD", "local-test-password")
    monkeypatch.setenv("IMAP_FOLDER", "INBOX")
    monkeypatch.setenv("USE_CELERY", "true")
    monkeypatch.setenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "0")
    monkeypatch.setenv("EMAIL_ALLOWED_SENDERS", "")
    monkeypatch.setenv("EMAIL_BLOCKED_SENDERS", "")
    monkeypatch.setenv("EMAIL_MAX_ATTACHMENT_MB", "20")
    monkeypatch.setattr(
        document_ingest_services,
        "upload_file",
        lambda key, body, kind: uploads.append((key, body, kind)),
    )
    monkeypatch.setattr(document_ingest_services, "delete_file", lambda *_: None)
    monkeypatch.setattr(
        document_ingest_services,
        "dispatch_document_processing",
        lambda *_: "celery",
    )
    monkeypatch.setattr(
        email_polling_services,
        "_dispatch_queued_notifications",
        lambda _db, document_id: notifications.append(document_id),
    )
    db.add(
        EmailPollState(
            mailbox="ap@test-mail.test", folder="INBOX", uidvalidity=7, last_uid=0
        )
    )
    return db, uploads, notifications


def _poll(db, messages, *, client_type=FakeIMAP, force_uids=None, **imap_kwargs):
    client = client_type(messages, **imap_kwargs)
    result = email_polling_services.poll_email_messages(
        db, lambda *_: client, sleep=lambda _: None, force_uids=force_uids
    )
    return result, client


def _state(db):
    return next(row for row in db.rows if isinstance(row, EmailPollState))


def _documents(db):
    return [row for row in db.rows if isinstance(row, Document)]


def _seed_processed(db, uid, message_id):
    db.add(
        EmailMessageRecord(
            message_id=message_id,
            uid=uid,
            mailbox="ap@test-mail.test",
            folder="INBOX",
            raw_object_key=f"emails/2026/10/{uid}.eml",
            status="processed",
            attempts=1,
        )
    )


def test_uid_checkpoint_only_processes_uids_after_checkpoint(
    polling_setup,
):
    db, _, _ = polling_setup
    state = _state(db)
    state.last_uid = 2
    _seed_processed(db, 1, "<one@vendor.test>")
    _seed_processed(db, 2, "<two@vendor.test>")
    messages = {
        1: _mail("<one@vendor.test>", [("a.pdf", PDF, "application", "pdf")]),
        2: _mail("<two@vendor.test>", [("b.pdf", PDF, "application", "pdf")]),
        3: _mail("<three@vendor.test>", [("c.pdf", PDF, "application", "pdf")]),
    }

    result, client = _poll(db, messages)

    assert result["attachments"] == 1
    assert result["skipped_known"] == 0
    assert result["duplicates"] == 0
    assert state.last_uid == 3
    assert [uid for uid, _ in client.fetch_queries] == [3]
    assert "UID 3:*" in client.search_queries
    assert client.selected_readonly


def test_manually_seen_mail_is_still_fetched_with_body_peek(polling_setup):
    db, _, _ = polling_setup
    result, client = _poll(
        db,
        {1: _mail(attachments=[("bill.pdf", PDF, "application", "pdf")])},
        seen={1},
    )
    assert result["attachments"] == 1
    assert client.fetch_queries == [(1, "(INTERNALDATE BODY.PEEK[])")]
    assert client.selected_readonly


def test_first_run_does_not_backfill_by_default(monkeypatch):
    db = FakeDB()
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER", "ap@test-mail.test")
    monkeypatch.setenv("IMAP_PASSWORD", "local-test-password")
    monkeypatch.setenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "0")
    result, client = _poll(
        db,
        {1: _mail(), 2: _mail("<two@vendor.test>")},
    )
    assert result["emails"] == 0
    assert _state(db).last_uid == 2
    assert client.fetch_queries == []
    assert not _documents(db)


def test_first_successful_poll_after_imap_setup_failure_does_not_backfill(
    monkeypatch,
):
    db = FakeDB()
    monkeypatch.setenv("IMAP_HOST", "")
    monkeypatch.setenv("IMAP_USER", "ap@test-mail.test")
    monkeypatch.setenv("IMAP_PASSWORD", "local-test-password")
    monkeypatch.setenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "0")

    unavailable = email_polling_services.poll_email_messages(db)
    assert unavailable["failed"] == 1
    assert _state(db).uidvalidity == 0

    monkeypatch.setenv("IMAP_HOST", "imap.test")
    result, client = _poll(
        db,
        {1: _mail(), 2: _mail("<two@vendor.test>")},
    )

    assert result["emails"] == 0
    assert _state(db).uidvalidity == 7
    assert _state(db).last_uid == 2
    assert client.fetch_queries == []
    assert not _documents(db)


def test_initial_lookback_ingests_only_matching_window(monkeypatch, polling_setup):
    db, _, _ = polling_setup
    db.rows.remove(_state(db))
    monkeypatch.setenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "24")
    result, _ = _poll(
        db,
        {1: _mail(attachments=[("bill.pdf", PDF, "application", "pdf")])},
    )
    assert result["attachments"] == 1
    assert _state(db).last_uid == 1


def test_initial_lookback_uses_imap_internal_date(polling_setup, monkeypatch):
    db, _, _ = polling_setup
    db.rows.remove(_state(db))
    monkeypatch.setenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "24")
    now = datetime.now(UTC)
    old = now - timedelta(hours=25)
    messages = {
        1: _mail("<old@vendor.test>", [("old.pdf", PDF, "application", "pdf")]),
        2: _mail("<recent@vendor.test>", [("recent.pdf", PDF, "application", "pdf")]),
    }
    result, _ = _poll(db, messages, internal_dates={1: old, 2: now})
    assert result["filtered"] == 1
    assert result["attachments"] == 1
    assert [
        row.email_message_id for row in _documents(db) if row.parent_document_id is None
    ] == ["<recent@vendor.test>"]


def test_uidvalidity_change_resets_then_replays_with_message_id_dedupe(polling_setup):
    db, _, _ = polling_setup
    state = _state(db)
    state.uidvalidity = 6
    state.last_uid = 100
    message = _mail(
        "<previously-seen@vendor.test>", [("bill.pdf", PDF, "application", "pdf")]
    )
    db.add(
        Document(
            id=uuid.uuid4(),
            original_filename="email.eml",
            file_hash=hashlib.sha256(message).hexdigest(),
            status="received",
            email_message_id="<previously-seen@vendor.test>",
        )
    )
    _seed_processed(db, 1, "<previously-seen@vendor.test>")
    result, _ = _poll(db, {1: message}, uidvalidity=7)
    assert state.uidvalidity == 7
    assert state.last_uid == 1
    assert result["duplicates"] == 1
    assert result["attachments"] == 0


def test_failure_blocks_checkpoint_past_failing_uid(polling_setup):
    db, _, _ = polling_setup
    state = _state(db)

    class FailsFirstUID(FakeIMAP):
        def uid(self, command, *args):
            if command == "fetch" and int(args[0]) == 1:
                return "NO", []
            return super().uid(command, *args)

    client = FailsFirstUID(
        {
            1: _mail("<failed@vendor.test>"),
            2: _mail("<later@vendor.test>", [("bill.pdf", PDF, "application", "pdf")]),
        }
    )
    result = email_polling_services.poll_email_messages(db, lambda *_: client)
    assert result["failed"] == 1
    assert result["attachments"] == 1
    assert any(
        row.email_message_id == "<later@vendor.test>"
        for row in _documents(db)
        if row.parent_document_id is None
    )
    assert state.last_uid == 0
    failed = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert failed.attempts == 1


def test_failed_uid_retries_with_backoff_then_is_skipped(polling_setup, monkeypatch):
    db, _, _ = polling_setup
    state = _state(db)

    class BrokenFetch(FakeIMAP):
        def uid(self, command, *args):
            if command == "fetch":
                return "NO", []
            return super().uid(command, *args)

    monkeypatch.setenv("EMAIL_MAX_ATTEMPTS", "3")
    for _ in range(3):
        row = next(
            (item for item in db.rows if isinstance(item, EmailMessageRecord)), None
        )
        if row:
            row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        email_polling_services.poll_email_messages(
            db, lambda *_: BrokenFetch({1: _mail()}), sleep=lambda _: None
        )
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.attempts == 3
    assert row.status == "failed_final"
    assert state.last_uid == 1


def test_transient_and_permanent_error_classification():
    assert email_polling_services._is_transient_email_error(
        TimeoutError("socket timed out")
    )
    assert email_polling_services._is_transient_email_error(
        RuntimeError("MinIO connection reset")
    )
    assert not email_polling_services._is_transient_email_error(
        ValueError("unparseable MIME message")
    )


def test_permanent_processing_error_is_final_without_retry(polling_setup, monkeypatch):
    db, _, _ = polling_setup

    def permanent_failure(*_args, **_kwargs):
        raise ValueError("Unparseable attachment payload")

    monkeypatch.setattr(email_polling_services, "ingest_file", permanent_failure)
    result, _ = _poll(
        db,
        {1: _mail(attachments=[("bill.pdf", PDF, "application", "pdf")])},
    )
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert result["failed"] == 1
    assert row.status == "failed_final"
    assert row.attempts == 1
    assert row.next_retry_at is None
    assert any(
        audit.action == "email_failed_final"
        for audit in db.rows
        if isinstance(audit, AuditLog)
    )


def test_retry_backoff_schedule_is_bounded_at_six_hours():
    now = datetime.now(UTC)
    delays = [
        (email_polling_services._retry_at(attempt) - now).total_seconds()
        for attempt in range(1, 6)
    ]
    expected = [300, 900, 3600, 21600, 21600]
    assert all(
        abs(actual - target) < 2
        for actual, target in zip(delays, expected, strict=True)
    )


def test_failed_uid_is_retried_and_committed_on_next_poll(polling_setup):
    db, _, _ = polling_setup
    state = _state(db)

    class FailsOnce(FakeIMAP):
        has_failed = False

        def uid(self, command, *args):
            if command == "fetch" and not type(self).has_failed:
                type(self).has_failed = True
                return "NO", []
            return super().uid(command, *args)

    message = _mail("<retry@vendor.test>", [("bill.pdf", PDF, "application", "pdf")])
    first, _ = _poll(db, {1: message}, client_type=FailsOnce)
    assert first["failed"] == 1
    assert state.last_uid == 0
    failed_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert failed_row.next_retry_at > datetime.now(UTC) + timedelta(minutes=4)

    too_soon, early_client = _poll(db, {1: message})
    assert too_soon["failed"] == 0
    assert not any("BODY.PEEK[]" in query for _, query in early_client.fetch_queries)
    assert failed_row.attempts == 1
    failed_row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)

    second, _ = _poll(db, {1: message})
    assert second["attachments"] == 1
    assert state.last_uid == 1
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.attempts == 2


def test_storage_failure_is_logged_sanitized_retried_and_does_not_block_batch(
    polling_setup, monkeypatch, caplog
):
    import logging

    db, uploads, _ = polling_setup
    state = _state(db)
    message = _mail(
        "<storage-failure@vendor.test>",
        [("broken.pdf", PDF, "application", "pdf")],
    )
    good_pdf = b"%PDF-1.7\n" + b"unique successful mail attachment " * 20
    good_message = _mail(
        "<same-batch-success@vendor.test>",
        [("good.pdf", good_pdf, "application", "pdf")],
    )
    real_upload = document_ingest_services.upload_file

    def failing_upload(key, body, content_type):
        if key.endswith("/broken.pdf"):
            raise RuntimeError(
                "MinIO connection failed; IMAP_PASSWORD=local-test-password; "
                "Authorization: Bearer private-token"
            )
        return real_upload(key, body, content_type)

    monkeypatch.setattr(document_ingest_services, "upload_file", failing_upload)

    with caplog.at_level(logging.ERROR, logger=email_polling_services.__name__):
        monkeypatch.setenv("EMAIL_MAX_ATTEMPTS", "5")
        for attempt in range(5):
            failed_before = next(
                (
                    row
                    for row in db.rows
                    if isinstance(row, EmailMessageRecord)
                    and row.message_id == "<storage-failure@vendor.test>"
                ),
                None,
            )
            if failed_before:
                failed_before.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            result, _ = _poll(db, {1: message, 2: good_message})
            assert result["failed"] == 1, (
                attempt,
                result,
                [
                    (row.message_id, row.status, row.attempts)
                    for row in db.rows
                    if isinstance(row, EmailMessageRecord)
                ],
                state.last_uid,
            )
            assert any(
                document.original_filename == "good.pdf" for document in _documents(db)
            )
            if attempt == 0:
                assert state.last_uid == 0
                failure = next(
                    row
                    for row in reversed(db.rows)
                    if isinstance(row, AuditLog) and row.action == "email_failed"
                )
                assert "RuntimeError" in failure.technical_detail
                assert "MinIO connection failed" in failure.technical_detail
                assert "[REDACTED]" in failure.technical_detail
                assert len(failure.technical_detail) <= 500
                assert failure.reason == failure.technical_detail

    assert "Traceback (most recent call last)" in caplog.text
    assert "MinIO connection failed" in caplog.text
    assert "local-test-password" not in caplog.text
    assert "private-token" not in caplog.text
    failed = next(
        row
        for row in db.rows
        if isinstance(row, EmailMessageRecord)
        and row.message_id == "<storage-failure@vendor.test>"
    )
    assert failed.attempts == 5
    assert failed.status == "failed_final"
    assert failed.failure_reason
    assert state.last_uid == 2
    assert any(
        row.action == "email_failed_final"
        for row in db.rows
        if isinstance(row, AuditLog)
    )
    assert any(key.endswith("/good.pdf") for key, _, _ in uploads)


@pytest.mark.parametrize(
    ("sender", "headers", "subject", "allowed", "blocked", "expected"),
    [
        ("ap@test-mail.test", {}, "Invoice", "", "", "sender_is_mailbox"),
        (
            "mailer-daemon@example.test",
            {},
            "Invoice",
            "",
            "",
            "blocked_sender",
        ),
        (
            "vendor@example.test",
            {"Auto-Submitted": "auto-replied"},
            "Invoice",
            "",
            "",
            "bulk_marketing",
        ),
        (
            "vendor@example.test",
            {"Precedence": "bulk"},
            "Invoice",
            "",
            "",
            "bulk_marketing",
        ),
        (
            "vendor@example.test",
            {},
            "Invoice",
            "other@example.test",
            "",
            "sender_not_allowlisted",
        ),
        (
            "vendor@example.test",
            {},
            "Invoice",
            "",
            "vendor@example.test",
            "blocked_sender",
        ),
    ],
)
def test_configurable_sender_and_automation_filters(
    polling_setup, monkeypatch, sender, headers, subject, allowed, blocked, expected
):
    db, _, _ = polling_setup
    monkeypatch.setenv("EMAIL_ALLOWED_SENDERS", allowed)
    monkeypatch.setenv("EMAIL_BLOCKED_SENDERS", blocked)
    result, _ = _poll(
        db, {1: _mail(sender=sender, extra_headers=headers, subject=subject)}
    )
    assert result["filtered"] == 1
    assert not _documents(db)
    assert any(
        isinstance(row, AuditLog)
        and row.action == "email_skipped_filtered"
        and row.technical_detail == expected
        for row in db.rows
    )


def test_sender_blocklist_matches_configured_domain_or_pattern(
    polling_setup, monkeypatch
):
    db, _, _ = polling_setup
    monkeypatch.setenv("EMAIL_SENDER_BLOCKLIST", "blocked.test,no-reply")
    result, _ = _poll(
        db,
        {
            1: _mail(
                "<blocked@vendor.test>",
                sender="ap@blocked.test",
                attachments=[("bill.pdf", PDF, "application", "pdf")],
            )
        },
    )
    assert result["attachments"] == 0
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.status == "ignored"
    assert row.filter_reason == "Sender is blocked by mailbox filters"
    assert not _documents(db)


def test_bounce_subject_and_auto_submitted_no_filter(polling_setup):
    db, _, _ = polling_setup
    result, _ = _poll(
        db,
        {
            1: _mail(
                "<bounce@vendor.test>",
                sender="billing@example.test",
                subject="Delivery Status Notification: failure",
            ),
            2: _mail(
                "<good@vendor.test>",
                [("bill.pdf", PDF, "application", "pdf")],
                extra_headers={"Auto-Submitted": "no"},
            ),
        },
    )
    assert result["filtered"] == 1
    assert result["attachments"] == 1


def test_bad_magic_attachment_is_rejected(polling_setup):
    db, uploads, _ = polling_setup
    result, _ = _poll(
        db, {1: _mail(attachments=[("bill.pdf", b"x" * 500, "application", "pdf")])}
    )
    email_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert email_row.status == "ignored"
    assert email_row.failure_reason == "Unsupported attachment type or file signature"
    assert result["attachments"] == 0
    assert result["failed"] == 0
    assert result["filtered"] == 1
    assert not _documents(db)
    assert not any(key.startswith("incoming/") for key, _, _ in uploads)


def test_valid_docx_email_attachment_uses_shared_ingest(polling_setup):
    db, uploads, _ = polling_setup
    payload = _docx_bytes()
    result, _ = _poll(
        db,
        {
            1: _mail(
                attachments=[
                    (
                        "bill.docx",
                        payload,
                        "application",
                        "vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                ]
            )
        },
    )

    assert result["attachments"] == 1
    child = next(row for row in _documents(db) if row.original_filename == "bill.docx")
    assert child.mime == DOCX_MIME
    assert child.status == "received"
    assert any(body == payload and mime == DOCX_MIME for _, body, mime in uploads)


@pytest.mark.parametrize("header_offset", [0, 16, 1023])
def test_pdf_header_within_allowed_preamble_is_accepted(header_offset):
    part = MIMEMessage()
    part["Content-Type"] = "application/pdf"
    payload = b" " * header_offset + b"%PDF-1.7\nbody"

    assert email_polling_services._attachment_format(part, payload) is not None


def test_pdf_header_after_allowed_preamble_is_rejected():
    part = MIMEMessage()
    part["Content-Type"] = "application/pdf"
    payload = b" " * 1024 + b"%PDF-1.7\nbody"

    assert email_polling_services._attachment_format(part, payload) is None


@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBP"),
        ("text/csv", b"employee_id,name\n1,Example"),
    ],
)
def test_misnamed_pdf_with_unsupported_content_is_rejected(content_type, payload):
    part = MIMEMessage()
    part["Content-Type"] = content_type
    part["Content-Disposition"] = 'attachment; filename="invoice.pdf"'

    assert email_polling_services._attachment_format(part, payload) is None


def test_oversized_attachment_is_failed_without_storage(polling_setup, monkeypatch):
    db, uploads, _ = polling_setup
    monkeypatch.setenv("EMAIL_MAX_ATTACHMENT_MB", "1")
    content = b"%PDF-1.7\n" + b"x" * (1024 * 1024)
    result, _ = _poll(
        db, {1: _mail(attachments=[("big.pdf", content, "application", "pdf")])}
    )
    email_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert email_row.status == "needs_attention"
    assert email_row.failure_reason == "Attachment exceeds the configured size limit"
    assert result["filtered"] == 1
    assert not _documents(db)
    assert not any(key.startswith("incoming/") for key, _, _ in uploads)


def test_inline_image_under_twenty_kb_is_skipped(polling_setup):
    db, _, _ = polling_setup
    result, _ = _poll(
        db, {1: _mail(attachments=[("pixel.png", PNG, "image", "png", "inline")])}
    )
    assert result["attachments"] == 0
    email_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert email_row.status == "ignored"
    assert email_row.raw_object_key is None
    assert email_row.failure_reason == "Only tiny inline images found"


def test_large_invoice_inline_image_is_ingested_as_email_document(polling_setup):
    db, _, _ = polling_setup
    image = _large_png()
    assert len(image) > 20 * 1024
    _poll(
        db,
        {1: _mail(attachments=[("invoice.png", image, "image", "png", "inline")])},
    )
    parent = next(
        row
        for row in db.rows
        if isinstance(row, Document) and row.parent_document_id is None
    )
    child = next(
        row
        for row in db.rows
        if isinstance(row, Document) and row.parent_document_id == parent.id
    )
    assert child.source == "email"
    assert child.mime == "image/png"
    assert child.email_sender == parent.email_sender
    assert child.email_subject == parent.email_subject
    assert child.email_message_id == parent.email_message_id


def test_at_most_ten_attachments_are_ingested(polling_setup):
    db, _, _ = polling_setup
    attachments = [
        (f"bill-{index}.pdf", PDF + str(index).encode(), "application", "pdf")
        for index in range(12)
    ]
    result, _ = _poll(db, {1: _mail(attachments=attachments)})
    assert result["attachments"] == 10
    assert result["failed"] == 2


def test_email_and_attachment_objects_use_safe_keys_and_db_metadata(polling_setup):
    db, uploads, _ = polling_setup
    raw = _mail(
        attachments=[("..\\..\\invoice weird!.pdf", PDF, "application", "pdf")],
        body="Invoice detail " * 300,
    )
    result, _ = _poll(db, {9: raw})
    parent = next(row for row in _documents(db) if row.parent_document_id is None)
    child = next(row for row in _documents(db) if row.parent_document_id == parent.id)
    message_hash = hashlib.sha1(parent.email_message_id.encode()).hexdigest()
    eml_key = f"emails/{parent.email_received_at:%Y/%m}/{message_hash}.eml"
    assert result["attachments"] == 1
    assert parent.status == "processed"
    assert parent.email_uid == 9
    assert parent.email_eml_key == eml_key
    assert len(parent.email_body_preview) <= 2000
    assert parent.email_body_preview.startswith("Invoice detail")
    assert parent.storage_path == eml_key
    assert child.storage_path.startswith("incoming/")
    assert ".." not in child.storage_path
    assert child.storage_path == (
        f"incoming/{parent.email_received_at:%Y/%m}/{child.id}/invoice_weird_.pdf"
    )
    assert child.parent_document_id == parent.id
    assert child.source == "email"
    assert child.email_message_id == parent.email_message_id
    assert child.email_sender == parent.email_sender
    assert child.email_subject == parent.email_subject
    assert any(key == eml_key and body == raw for key, body, _ in uploads)
    assert any(key == child.storage_path for key, _, _ in uploads)


def test_retried_email_reuses_existing_documents_without_duplicates(polling_setup):
    db, _, _ = polling_setup
    message = _mail(attachments=[("bill.pdf", PDF, "application", "pdf")])
    _poll(db, {1: message})
    initial_documents = len(_documents(db))
    email_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    email_row.status = "failed"
    email_row.attempts = 0
    email_row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    result, _ = _poll(db, {1: message})
    assert len(_documents(db)) == initial_documents
    assert result["duplicates"] == 1
    assert email_row.status == "processed"


def test_ingest_root_integrity_conflict_is_treated_as_duplicate(
    polling_setup, monkeypatch
):
    from sqlalchemy.exc import IntegrityError

    db, _, _ = polling_setup
    message_id = "<duplicate-root@vendor.test>"
    existing = Document(
        id=uuid.uuid4(),
        original_filename="email.eml",
        file_hash="d" * 64,
        status="processed",
        source="email",
        email_message_id=message_id,
        parent_document_id=None,
    )
    db.add(existing)
    add = db.add

    def conflicting_add(row):
        if (
            isinstance(row, Document)
            and row.email_message_id == message_id
            and row.parent_document_id is None
        ):
            raise IntegrityError("insert", {}, Exception("unique root"))
        add(row)

    monkeypatch.setattr(db, "add", conflicting_add)
    result = document_ingest_services.ingest_file(
        db,
        "email.eml",
        b"different raw email bytes",
        "message/rfc822",
        None,
        "email",
        email_meta={"email_message_id": message_id},
    )
    assert result.status == "duplicate_blocked"
    assert result.document_id == existing.id
    assert len(_documents(db)) == 1


def test_admin_email_retry_resets_attempts_and_audits(polling_setup, monkeypatch):
    from fastapi import BackgroundTasks

    from src.api.rest.routers.email_polling_routers import retry_email_message
    from src.worker.tasks import retry_email_message as retry_task

    db, _, _ = polling_setup
    row = EmailMessageRecord(
        id=uuid.uuid4(),
        message_id="<retry@vendor.test>",
        uid=9,
        mailbox="ap@test-mail.test",
        sender="billing@vendor.test",
        status="failed_final",
        failure_reason="Connection failed",
        attempts=5,
    )
    db.add(row)
    dispatched = []
    monkeypatch.setattr(retry_task, "delay", lambda *_args: dispatched.append(True))
    monkeypatch.setenv("USE_CELERY", "true")
    result = retry_email_message(
        message_id=row.id,
        background_tasks=BackgroundTasks(),
        db=db,
        current_user={"role": "admin", "sub": str(uuid.uuid4())},
    )
    assert result["status"] == "failed"
    assert row.attempts == 0
    assert row.failure_reason is None
    assert dispatched == [True]
    assert any(
        audit.action == "email_retry_requested"
        for audit in db.rows
        if isinstance(audit, AuditLog)
    )


def test_failed_retry_records_fresh_reason_instead_of_backfill_placeholder(
    polling_setup, monkeypatch
):
    from fastapi import BackgroundTasks

    from src.api.rest.routers.email_polling_routers import retry_email_message

    db, _, _ = polling_setup
    row = EmailMessageRecord(
        id=uuid.uuid4(),
        message_id="<retry-real-reason@vendor.test>",
        uid=12,
        mailbox="ap@test-mail.test",
        status="failed_final",
        failure_reason="Unknown (before reason tracking)",
        attempts=5,
    )
    db.add(row)
    monkeypatch.setenv("USE_CELERY", "false")
    retry_email_message(
        message_id=row.id,
        background_tasks=BackgroundTasks(),
        db=db,
        current_user={"role": "admin", "sub": str(uuid.uuid4())},
    )
    raw = _mail(
        message_id=row.message_id,
        attachments=[("bill.pdf", b"not a pdf", "application", "pdf")],
    )
    _poll(db, {12: raw})
    assert row.status == "ignored"
    assert row.failure_reason != "Unknown (before reason tracking)"
    assert "Unsupported attachment" in row.failure_reason


def test_forced_retry_processes_uid_older_than_checkpoint_overlap(polling_setup):
    db, _, _ = polling_setup
    state = _state(db)
    state.last_uid = 100
    row = EmailMessageRecord(
        message_id="<old-retry@vendor.test>",
        uid=1,
        mailbox="ap@test-mail.test",
        folder="INBOX",
        status="failed",
        attempts=0,
    )
    db.add(row)
    raw = _mail(
        message_id=row.message_id,
        attachments=[("old-invoice.pdf", PDF, "application", "pdf")],
    )
    result, _ = _poll(db, {1: raw}, force_uids={1})
    assert result["attachments"] == 1
    assert row.status == "processed"
    assert state.last_uid == 100


def test_stuck_document_sweep_marks_failed_and_propagates_reason(
    polling_setup, monkeypatch
):
    from src.services.document_recovery_services import sweep_stuck_documents

    db, _, _ = polling_setup
    old = datetime.now(UTC) - timedelta(minutes=90)
    document = Document(
        id=uuid.uuid4(),
        original_filename="stuck.pdf",
        file_hash="b" * 64,
        status="received",
        source="email",
        email_message_id="<stuck@vendor.test>",
        updated_at=old,
        retry_count=3,
    )
    message = EmailMessageRecord(
        message_id="<stuck@vendor.test>",
        uid=4,
        mailbox="ap@test-mail.test",
        sender="billing@vendor.test",
        status="processed",
        attempts=1,
    )
    db.add(document)
    db.add(message)
    db.add(
        AuditLog(
            action="document_stuck_requeued",
            entity_type="document",
            entity_id=document.id,
            result="success",
            message="previous retry",
        )
    )
    monkeypatch.setenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15")
    assert sweep_stuck_documents(db) == 1
    assert document.status == "failed"
    assert document.failure_class == "retry_exhausted"
    assert "three automatic retries" in document.failure_message
    assert message.status == "failed_final"
    assert message.failure_reason == document.failure_message


def test_sweep_requeues_old_received_document_on_documents_queue(
    polling_setup, monkeypatch
):
    from src.services.document_recovery_services import sweep_stuck_documents
    from src.worker.tasks import process_document

    db, _, _ = polling_setup
    document = Document(
        id=uuid.uuid4(),
        original_filename="stuck.pdf",
        file_hash="c" * 64,
        status="received",
        source="upload",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    db.add(document)
    queued = []
    monkeypatch.setattr(
        process_document,
        "apply_async",
        lambda **kwargs: queued.append(kwargs),
    )
    monkeypatch.setenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15")
    assert sweep_stuck_documents(db) == 1
    assert document.status == "failed"
    assert document.failure_class == "retryable"
    assert document.retry_count == 1
    assert document.next_retry_at is not None
    assert queued == [
        {
            "args": [str(document.id)],
            "queue": "documents",
            "countdown": 30,
        }
    ]


def test_stuck_raw_email_without_attachment_gets_attention_reason(
    polling_setup, monkeypatch
):
    from src.services.document_recovery_services import sweep_stuck_documents
    from src.storage import minio_storage

    db, _, _ = polling_setup
    raw = _mail(message_id="<stuck-no-attachment@vendor.test>")
    document = Document(
        id=uuid.uuid4(),
        original_filename="email.eml",
        file_hash=hashlib.sha256(raw).hexdigest(),
        storage_path="emails/2026/10/stuck.eml",
        mime="message/rfc822",
        status="received",
        source="email",
        email_message_id="<stuck-no-attachment@vendor.test>",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    message = EmailMessageRecord(
        message_id=document.email_message_id,
        uid=10,
        mailbox="ap@test-mail.test",
        status="failed_final",
    )
    db.add(document)
    db.add(message)
    monkeypatch.setattr(minio_storage, "download_file", lambda _key: raw)
    monkeypatch.setenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15")
    assert sweep_stuck_documents(db) == 1
    assert document.status == "ignored"
    assert document.failure_message == "No attachment found"
    assert message.status == "needs_attention"
    assert message.failure_reason == "No attachment found"


def test_stuck_raw_email_recovers_inline_invoice_image(polling_setup, monkeypatch):
    from src.services.document_recovery_services import sweep_stuck_documents
    from src.storage import minio_storage

    db, _, _ = polling_setup
    raw = _mail(
        message_id="<stuck-inline-image@vendor.test>",
        attachments=[("invoice.png", _large_png(), "image", "png", "inline")],
    )
    document = Document(
        id=uuid.uuid4(),
        original_filename="email.eml",
        file_hash=hashlib.sha256(raw).hexdigest(),
        storage_path="emails/2026/10/stuck-inline.eml",
        mime="message/rfc822",
        status="received",
        source="email",
        email_sender="billing@vendor.test",
        email_subject="Invoice screenshot",
        email_message_id="<stuck-inline-image@vendor.test>",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    message = EmailMessageRecord(
        message_id=document.email_message_id,
        uid=11,
        mailbox="ap@test-mail.test",
        status="failed_final",
    )
    db.add(document)
    db.add(message)
    monkeypatch.setattr(minio_storage, "download_file", lambda _key: raw)
    monkeypatch.setenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15")
    assert sweep_stuck_documents(db) == 1
    child = next(
        row
        for row in db.rows
        if isinstance(row, Document) and row.parent_document_id == document.id
    )
    assert child.original_filename == "invoice.png"
    assert child.mime == "image/png"
    assert child.source == "email"
    assert child.email_sender == document.email_sender
    assert child.email_subject == document.email_subject
    assert child.email_message_id == document.email_message_id
    assert message.status == "processed"


def test_celery_task_routes_match_compose_worker_queues():
    from src.worker.celery_app import celery_app

    routes = celery_app.conf.task_routes
    assert routes["ap.poll_email"]["queue"] == "email"
    assert routes["ap.retry_email_message"]["queue"] == "email"
    assert routes["ap.send_notification"]["queue"] == "email"
    assert routes["ap.run_follow_ups"]["queue"] == "email"
    assert routes["ap.process_document"]["queue"] == "documents"
    assert routes["ap.sweep_stuck_documents"]["queue"] == "documents"
    assert (
        celery_app.conf.beat_schedule["sweep-stuck-documents-every-five-minutes"][
            "task"
        ]
        == "ap.sweep_stuck_documents"
    )
    assert (
        celery_app.conf.beat_schedule["sweep-stuck-documents-every-five-minutes"][
            "schedule"
        ].run_every.total_seconds()
        == 300
    )


def test_html_body_is_stripped_when_no_plain_text(polling_setup):
    db, _, _ = polling_setup
    message = MIMEMessage()
    message["From"] = "billing@example.test"
    message["To"] = "ap@test-mail.test"
    message["Subject"] = "Invoice"
    message["Message-ID"] = "<html@vendor.test>"
    message["Date"] = format_datetime(datetime.now(UTC))
    message.set_content(
        "<html><body><style>hidden</style><p>Readable &amp; clear</p></body></html>",
        subtype="html",
    )
    _poll(db, {1: message.as_bytes()})
    email_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert email_row.body_preview == "Readable & clear"
    assert email_row.raw_object_key is None


def test_no_attachment_queues_allowlist_protected_notification(polling_setup):
    db, _, notifications = polling_setup
    result, _ = _poll(db, {1: _mail()})
    email_row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert email_row.status == "needs_attention"
    assert email_row.raw_object_key is None
    assert email_row.failure_reason == "No attachment found"
    assert email_row.filter_reason == "No attachment found"
    assert not _documents(db)
    assert not any(isinstance(row, Notification) for row in db.rows)
    assert notifications == []
    assert result["attachments"] == 0


def test_uid_fetch_failure_does_not_leak_imap_secret(polling_setup, caplog):
    db, _, _ = polling_setup
    secret = "should-never-appear-in-logs"

    class BrokenLogin:
        def __init__(self, *_):
            pass

        def login(self, *_):
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] {secret}")

        def logout(self):
            pass

    result = email_polling_services.poll_email_messages(
        db, BrokenLogin, sleep=lambda _: None
    )
    assert result["message"] == email_polling_services.IMAP_AUTH_ERROR
    assert secret not in caplog.text
    assert secret not in str(result)
    assert not any(isinstance(row, EmailMessageRecord) for row in db.rows)
    assert all(
        secret not in (row.technical_detail or "")
        for row in db.rows
        if isinstance(row, AuditLog)
    )


def test_beat_schedule_requires_enabled_imap_provider(monkeypatch):
    import importlib

    from src.worker import celery_app as celery_module

    monkeypatch.setenv("EMAIL_POLL_ENABLED", "true")
    monkeypatch.setenv("EMAIL_PROVIDER", "imap")
    enabled = importlib.reload(celery_module)
    assert "poll-ap-email" in enabled.celery_app.conf.beat_schedule
    monkeypatch.setenv("EMAIL_PROVIDER", "other")
    disabled = importlib.reload(celery_module)
    assert "poll-ap-email" not in disabled.celery_app.conf.beat_schedule


def test_status_endpoint_returns_persisted_uid_state(polling_setup):
    from datetime import UTC, datetime

    db, _, _ = polling_setup
    state = _state(db)
    state.last_uid = 42
    state.last_run_at = datetime.now(UTC)
    state.last_result = {
        "emails": 4,
        "attachments": 2,
        "duplicates": 1,
        "filtered": 1,
        "failed": 0,
        "filtered_by_reason": {"bulk_marketing": 1},
    }
    payload = email_polling_routers.email_poll_status(db=db, _user={"role": "admin"})
    assert payload["last_uid"] == 42
    assert payload["uidvalidity"] == 7
    assert payload["last_run_at"]
    assert payload["last_result"] == state.last_result
    assert payload["filtered_by_reason"] == {"bulk_marketing": 1}


def test_unknown_no_reply_pdf_is_skipped(polling_setup, monkeypatch):
    db, _, _ = polling_setup
    result, _ = _poll(
        db,
        {
            1: _mail(
                sender="noreply@unknown.test",
                attachments=[("bill.pdf", PDF, "application", "pdf")],
            )
        },
    )
    assert result["attachments"] == 0
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.status == "ignored"
    assert row.filter_reason == "Sender is blocked by mailbox filters"
    assert not _documents(db)


def test_unknown_bulk_without_pdf_is_ignored_with_reason(polling_setup):
    db, uploads, _ = polling_setup
    result, _ = _poll(
        db,
        {
            1: _mail(
                sender="promo@marketing.test",
                extra_headers={"List-Unsubscribe": "<mailto:stop@marketing.test>"},
            )
        },
    )
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.status == "ignored"
    assert row.filter_reason == "Bulk or marketing email was skipped"
    assert result["filtered_by_reason"] == {"bulk_marketing": 1}
    assert not uploads and not _documents(db)


def test_known_vendor_does_not_bypass_blocklist_or_bulk_filters(
    polling_setup, monkeypatch
):
    db, _, _ = polling_setup
    vendor = Vendor(
        name="Trusted", contact_email="noreply@trusted.test", is_active=True
    )
    db.add(vendor)
    db.add(
        VendorAlias(
            vendor_id=vendor.id or uuid.uuid4(),
            alias_norm="trusted.test",
            is_active=True,
        )
    )
    monkeypatch.setenv("EMAIL_SENDER_BLOCKLIST", "noreply")
    result, _ = _poll(
        db,
        {
            1: _mail(
                sender="noreply@trusted.test",
                attachments=[("bill.pdf", PDF, "application", "pdf")],
                extra_headers={"Precedence": "bulk"},
            )
        },
    )
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.status == "ignored"
    assert row.filter_reason == "Sender is blocked by mailbox filters"
    assert result["attachments"] == 0


def test_vendor_alias_cannot_bypass_noreply_filter(polling_setup, monkeypatch):
    db, _, _ = polling_setup
    vendor = Vendor(
        name="Trusted", contact_email="billing@trusted.test", is_active=True
    )
    db.add(vendor)
    db.add(
        VendorAlias(
            vendor_id=vendor.id or uuid.uuid4(),
            alias_norm="trusted billing",
            is_active=True,
        )
    )
    monkeypatch.setenv("EMAIL_SENDER_BLOCKLIST", "noreply")
    result, _ = _poll(
        db,
        {
            1: _mail(
                sender="Trusted Billing <noreply@unknown.test>",
                attachments=[("bill.pdf", PDF, "application", "pdf")],
            )
        },
    )
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.status == "ignored"
    assert row.filter_reason == "Sender is blocked by mailbox filters"
    assert result["attachments"] == 0


def test_no_reply_never_gets_no_attachment_notification(polling_setup, monkeypatch):
    db, _, notifications = polling_setup
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "noreply@sender.test")
    db.add(Vendor(name="Trusted", contact_email="noreply@sender.test", is_active=True))
    _poll(db, {1: _mail(sender="noreply@sender.test")})
    assert not any(isinstance(row, Notification) for row in db.rows)
    assert notifications == []


def test_bulk_image_only_is_ignored_but_vendor_image_is_accepted(polling_setup):
    db, _, _ = polling_setup
    result, _ = _poll(
        db,
        {
            1: _mail(
                sender="promo@bulk.test",
                extra_headers={"Feedback-ID": "x"},
                attachments=[("photo.png", PNG, "image", "png")],
            )
        },
    )
    assert result["filtered_by_reason"] == {"bulk_marketing": 1}
    assert not _documents(db)


def test_small_regular_attached_invoice_image_is_accepted(polling_setup):
    db, _, _ = polling_setup
    _poll(db, {1: _mail(attachments=[("tiny.png", PNG, "image", "png", "attachment")])})
    row = next(row for row in db.rows if isinstance(row, EmailMessageRecord))
    assert row.status == "processed"
    assert row.raw_object_key


def test_cid_and_tracking_pixel_images_are_skipped():
    message = MIMEMessage()
    message.set_content('<html><img src="cid:logo@test"></html>', subtype="html")
    message.add_related(PNG, maintype="image", subtype="png", cid="<logo@test>")
    assert not email_polling_services._has_valid_attachment(
        email_polling_services.email.message_from_bytes(message.as_bytes())
    )

    from email.message import Message

    pixel = Message()
    pixel["Content-Type"] = "image/png"
    pixel.set_payload(
        b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") * 2 + b"x" * 6000
    )
    assert email_polling_services._skip_image_part(
        pixel, pixel.get_payload(decode=True), set()
    )


def test_other_documents_are_reclassifiable(polling_setup, monkeypatch):
    from fastapi import BackgroundTasks

    from src.api.rest.routers import document_routers

    db, _, _ = polling_setup
    document = Document(
        id=uuid.uuid4(),
        original_filename="bulk.pdf",
        file_hash="a" * 64,
        storage_path="other/2026/10/id/bulk.pdf",
        status="other",
        doc_type="other",
        source="email_poll",
        source_flag="bulk_sender",
        failure_message="bulk sender, not a bill",
    )
    db.add(document)
    monkeypatch.setattr(document_routers, "move_file", lambda *_: None)
    monkeypatch.setattr(
        document_routers, "dispatch_document_processing", lambda *_: None
    )
    response = document_routers.reclassify_other_document(
        document_id=document.id,
        doc_type="invoice",
        background_tasks=BackgroundTasks(),
        db=db,
        current_user={"sub": str(uuid.uuid4())},
    )
    assert response["status"] == "received"
    assert document.source_flag is None
    assert document.classify_override == "invoice"
    assert document.failure_message is None


def test_email_message_list_exposes_filter_reason(polling_setup):
    from src.api.rest.routers.email_polling_routers import email_poll_messages

    db, _, _ = polling_setup
    row = EmailMessageRecord(
        message_id="<filtered@bulk.test>",
        uid=1,
        mailbox="ap@test-mail.test",
        sender="promo@bulk.test",
        status="ignored",
        filter_reason="bulk_marketing",
    )
    db.add(row)
    result = email_poll_messages(db=db, _user={"role": "admin"})
    assert result["items"][0]["filter_reason"] == "bulk_marketing"


@pytest.mark.parametrize(
    ("ui_status", "stored_statuses"),
    [("skipped", ["ignored"]), ("failed", ["failed", "failed_final"])],
)
def test_email_message_ui_status_filters_cover_stored_statuses(
    ui_status, stored_statuses
):
    from src.api.rest.routers.email_polling_routers import email_poll_messages

    query = MagicMock()
    query.filter.return_value = query
    query.count.return_value = 0
    page = query.order_by.return_value.offset.return_value.limit.return_value
    page.all.return_value = []
    db = MagicMock()
    db.query.return_value = query

    result = email_poll_messages(
        status=ui_status, db=db, _user={"role": "associate"}
    )

    assert result["total"] == 0
    expression = query.filter.call_args.args[0]
    for stored_status in stored_statuses:
        assert stored_status in str(expression.compile().params.values())


def test_manual_poll_endpoint_allows_admin_and_associate(
    monkeypatch, user_stub_factory
):
    secret = "email-poll-test-secret-with-32-bytes"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    db = MagicMock()
    associate_user = user_stub_factory(role="associate")
    admin_user = user_stub_factory(role="admin")
    users = {associate_user.id: associate_user, admin_user.id: admin_user}
    db.get.side_effect = lambda model, user_id: (
        users.get(user_id) if model is User else None
    )
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    monkeypatch.setattr(
        email_polling_routers,
        "poll_email_with_lock",
        lambda _db: {**email_polling_services.EMPTY_COUNTS},
    )
    try:
        associate = jwt.encode(
            {"sub": str(associate_user.id), "type": "access", "role": "associate"},
            secret,
            algorithm="HS256",
        )
        admin = jwt.encode(
            {"sub": str(admin_user.id), "type": "access", "role": "admin"},
            secret,
            algorithm="HS256",
        )
        associate_response = client.post(
            "/api/email-polling/run",
            headers={"Authorization": f"Bearer {associate}"},
        )
        assert associate_response.status_code == 200
        response = client.post(
            "/api/email-polling/run",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert response.status_code == 200
        assert response.json()["emails"] == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_status_endpoint_allows_admin_and_associate(monkeypatch, user_stub_factory):
    secret = "email-poll-status-secret-with-32-bytes"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    db = MagicMock()
    associate_user = user_stub_factory(role="associate")
    admin_user = user_stub_factory(role="admin")
    users = {associate_user.id: associate_user, admin_user.id: admin_user}
    db.get.side_effect = lambda model, user_id: (
        users.get(user_id) if model is User else None
    )
    db.query.return_value.filter_by.return_value.first.return_value = None
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    try:
        associate = jwt.encode(
            {"sub": str(associate_user.id), "type": "access", "role": "associate"},
            secret,
            algorithm="HS256",
        )
        admin = jwt.encode(
            {"sub": str(admin_user.id), "type": "access", "role": "admin"},
            secret,
            algorithm="HS256",
        )
        associate_response = client.get(
            "/api/email-polling/status",
            headers={"Authorization": f"Bearer {associate}"},
        )
        assert associate_response.status_code == 200
        response = client.get(
            "/api/email-polling/status",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert response.status_code == 200
        assert response.json()["last_result"]["filtered"] == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
