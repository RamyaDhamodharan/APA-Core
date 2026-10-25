"""Read-only Gmail IMAP polling with UID checkpoints and safe attachment intake."""

import contextlib
import email
import email.utils
import hashlib
import html.parser
import imaplib
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import Message
from pathlib import PurePath, PureWindowsPath

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage, EmailPollState
from src.models.notification_models import Notification
from src.models.vendor_models import Vendor, VendorAlias
from src.services.document_ingest_services import ingest_file
from src.services.docx_validation_services import DOCX_MIME_TYPE, is_valid_docx
from src.services.email_sender_services import is_automated_sender
from src.services.vendor_resolution_services import normalize_vendor_name

logger = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 5
RETRY_DELAYS_MINUTES = (5, 15, 60, 360)
MAX_ATTACHMENTS_PER_EMAIL = 10
INLINE_IMAGE_MIN_BYTES = 20 * 1024
IMAP_AUTH_ERROR = "Gmail rejected the login: use an App Password and enable IMAP"
IMAP_REACHABILITY_ERROR = "Cannot reach IMAP host"
IMAP_CONFIG_ERROR = "IMAP_USER / IMAP_PASSWORD not set"
EMPTY_COUNTS = {
    "found": 0,
    "emails": 0,
    "new": 0,
    "attachments": 0,
    "documents_created": 0,
    "duplicates": 0,
    "filtered": 0,
    "failed": 0,
    "skipped_known": 0,
    "filtered_by_reason": {},
    "skipped_by_reason": {},
    "failed_by_reason": {},
}
ATTACHMENT_FORMATS = {
    # ISO 32000 allows a short preamble before the PDF header. Keep the
    # bounded search so a renamed non-PDF cannot pass signature validation.
    "application/pdf": (
        ".pdf",
        lambda data: data.find(b"%PDF-", 0, 1024 + len(b"%PDF-") - 1) >= 0,
    ),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/jpg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
}
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|"
    r"imap_password\s*[:=]\s*|"
    r"(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)")


def _sanitize_exception_message(message: str) -> str:
    message = _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", str(message))
    message = _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", message)
    for name in (
        "IMAP_PASSWORD",
        "MINIO_SECRET_KEY",
        "JWT_SECRET_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
    ):
        secret = os.getenv(name, "")
        if secret and len(secret) >= 4:
            message = message.replace(secret, "[REDACTED]")
    return message[:500]


def _email_exception_detail(uid: int, exc: Exception) -> str:
    message = _sanitize_exception_message(str(exc)) or "No exception message"
    return f"UID {uid}: {type(exc).__name__}: {message}"[:500]


def _log_email_exception(uid: int, exc: Exception) -> None:
    safe_exception = RuntimeError(
        f"{type(exc).__name__}: "
        f"{_sanitize_exception_message(str(exc)) or 'No exception message'}"
    )
    logger.exception(
        "Email processing failed for UID %s",
        uid,
        exc_info=(RuntimeError, safe_exception, exc.__traceback__),
    )


def _friendly_imap_error(exc: Exception) -> str:
    if "AUTHENTICATIONFAILED" in str(exc).upper():
        return IMAP_AUTH_ERROR
    return IMAP_REACHABILITY_ERROR


def _audit(
    db, action, message, *, result="success", entity_id=None, detail=None, reason=None
):
    db.add(
        AuditLog(
            action=action,
            entity_type="email",
            entity_id=entity_id,
            result=result,
            message=message,
            reason=reason,
            technical_detail=detail,
        )
    )


class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _metadata(raw: bytes) -> tuple[Message, str, str, str, datetime | None, str]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = str(msg.get("Message-ID", "")).strip()
    sender = email.utils.parseaddr(str(msg.get("From", "")))[1].strip()
    subject = str(msg.get("Subject", ""))[:500]
    received = None
    try:
        parsed = email.utils.parsedate_to_datetime(str(msg.get("Date", "")))
        received = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        pass
    if not message_id:
        message_id = f"<raw-{hashlib.sha256(raw).hexdigest()}@local>"

    plain_parts = []
    html_parts = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_filename():
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, TypeError):
            continue
        if not isinstance(content, str):
            continue
        if part.get_content_type() == "text/plain":
            plain_parts.append(content)
        elif part.get_content_type() == "text/html":
            html_parts.append(content)
    if plain_parts:
        body = "\n".join(plain_parts)
    else:
        parser = _HTMLText()
        for content in html_parts:
            parser.feed(content)
        body = " ".join(" ".join(parser.parts).split())
    return msg, message_id, sender, subject, received, body[:2000]


def _uidvalidity(client) -> int:
    response = client.response("UIDVALIDITY")
    values = response[1] if response else []
    if not values:
        raise RuntimeError("IMAP UIDVALIDITY was unavailable")
    value = values[0]
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    return int(value)


class IMAPConnectionError(RuntimeError):
    """A mailbox connection failure that must fail the whole poll run."""


def _is_imap_connection_failure(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            IMAPConnectionError,
            OSError,
            TimeoutError,
            ConnectionError,
            imaplib.IMAP4.abort,
        ),
    )


def _fetch_message_id_header(client, uid: int) -> str | None:
    status, response = client.uid(
        "fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
    )
    if status != "OK" or not response:
        raise IMAPConnectionError("Could not fetch IMAP Message-ID header")
    raw = next((item[1] for item in response if isinstance(item, tuple)), None)
    if not isinstance(raw, bytes):
        return None
    parsed = email.message_from_bytes(raw, policy=policy.default)
    return str(parsed.get("Message-ID", "")) or None


def _max_attempts() -> int:
    try:
        return max(1, int(os.getenv("EMAIL_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def _is_transient_email_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, imaplib.IMAP4.abort)):
        return True
    if getattr(exc, "connection_invalidated", False):
        return True
    class_name = type(exc).__name__.casefold()
    if any(
        hint in class_name
        for hint in (
            "operationalerror",
            "interfaceerror",
            "connecttimeout",
            "readtimeout",
            "protocolerror",
            "connectionclosed",
            "serviceunavailable",
        )
    ):
        return True
    message = str(exc).casefold()
    return any(
        hint in message
        for hint in (
            "connection refused",
            "connection reset",
            "connection aborted",
            "connection timed out",
            "timed out",
            "ssl eof",
            "unexpected eof",
            "temporarily unavailable",
            "server disconnected",
            "broken pipe",
            "minio",
            "database connection",
            "could not connect",
            "connection is closed",
        )
    )


def _retry_at(attempt_no: int) -> datetime:
    index = min(max(attempt_no - 1, 0), len(RETRY_DELAYS_MINUTES) - 1)
    return datetime.now(UTC) + timedelta(minutes=RETRY_DELAYS_MINUTES[index])


def _record_email_failure(db, row, uid: int, counts: dict, exc: Exception) -> bool:
    attempt_no = row.attempts or 1
    transient = _is_transient_email_error(exc)
    final = not transient or attempt_no >= _max_attempts()
    row.status = "failed_final" if final else "failed"
    reason = _sanitize_exception_message(str(exc)) or type(exc).__name__
    if final and attempt_no >= _max_attempts():
        reason = f"Maximum retry attempts reached ({_max_attempts()}): {reason}"
    row.failure_reason = reason[:200]
    failed_by_reason = counts.setdefault("failed_by_reason", {})
    failed_by_reason[reason[:200]] = failed_by_reason.get(reason[:200], 0) + 1
    row.next_retry_at = None if final else _retry_at(attempt_no)
    retry_text = row.next_retry_at.isoformat() if row.next_retry_at else "none"
    detail = (
        f"{_email_exception_detail(uid, exc)}; attempt={attempt_no}; "
        f"next_retry_at={retry_text}"
    )[:500]
    _audit(
        db,
        "email_failed_final" if final else "email_failed",
        (
            "Email processing failed permanently"
            if final
            else "Email processing failed; retry scheduled"
        ),
        result="error",
        entity_id=row.id,
        detail=detail,
        reason=detail,
    )
    db.commit()
    if counts.get("failed", 0) == 0:
        counts["failed"] = 1
    return final


def _uid_search(client, criteria: str) -> list[int]:
    status, data = client.uid("search", None, criteria)
    if status != "OK":
        raise RuntimeError("Could not search IMAP UIDs")
    values = data[0].split() if data and data[0] else []
    return sorted({int(value) for value in values})


def _internal_date(response) -> datetime | None:
    for item in response:
        if not isinstance(item, tuple) or not item:
            continue
        header = item[0]
        if isinstance(header, bytes):
            header = header.decode("ascii", errors="ignore")
        match = re.search(r'INTERNALDATE\s+"([^"]+)"', str(header), re.IGNORECASE)
        if not match:
            continue
        try:
            value = email.utils.parsedate_to_datetime(match.group(1))
        except (TypeError, ValueError, OverflowError):
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return None


def _count_result(counts: dict) -> dict:
    return {
        key: (
            dict(counts.get(key, {}))
            if key in {"filtered_by_reason", "skipped_by_reason", "failed_by_reason"}
            else int(counts.get(key, 0))
        )
        for key in EMPTY_COUNTS
    }


def _empty_counts() -> dict:
    return {
        **EMPTY_COUNTS,
        "filtered_by_reason": {},
        "skipped_by_reason": {},
        "failed_by_reason": {},
    }


def _count_filtered(counts: dict, reason: str) -> None:
    counts["filtered"] += 1
    by_reason = counts.setdefault("filtered_by_reason", {})
    by_reason[reason] = by_reason.get(reason, 0) + 1
    skipped = counts.setdefault("skipped_by_reason", {})
    skipped[reason] = skipped.get(reason, 0) + 1


def _record_run(
    db,
    state,
    counts: dict,
    *,
    action="email_poll_run",
    message="Email polling completed",
    audit_result="success",
    audit_detail=None,
    started_at=None,
):
    result = _count_result(counts)
    if started_at is not None:
        result["duration_ms"] = int((time.monotonic() - started_at) * 1000)
    result["mails_found"] = result.get("found", 0)
    result["skipped"] = result.get("filtered", 0)
    result["new_documents"] = result.get("documents_created", 0)
    now = datetime.now(UTC)
    state.last_run_at = now
    state.last_result = result
    state.updated_at = now
    _audit(
        db,
        action,
        message,
        result=audit_result,
        detail=audit_detail or str(result),
    )
    db.commit()
    return result


def poll_email_with_lock(db, *, force_uids: set[int] | None = None) -> dict:
    """Run the poll under the existing shared Redis lock."""
    from redis import Redis

    started_at = time.monotonic()
    lock = None
    try:
        lock = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0")).lock(
            "ap:email-poll-lock", timeout=300, blocking_timeout=0
        )
        if not lock.acquire(blocking=False):
            return {**_empty_counts(), "skipped": "lock"}
        result = poll_email_messages(db, force_uids=force_uids)
        logger.info(
            "email_poll_run %s",
            json.dumps(
                {
                    **_count_result(result),
                    "mails_found": result.get("found", 0),
                    "skipped": result.get("filtered", 0),
                    "new_documents": result.get("documents_created", 0),
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "outcome": "failed" if result.get("failed") else "completed",
                },
                sort_keys=True,
            ),
        )
        return result
    except Exception:
        logger.warning("Email polling Redis lock is unavailable")
        logger.info(
            "email_poll_run %s",
            json.dumps(
                {
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "failed": 1,
                    "failure_reason": "Redis lock unavailable",
                },
                sort_keys=True,
            ),
        )
        _audit(
            db,
            "email_failed",
            "Email polling could not start",
            result="error",
            detail="Redis lock unavailable",
        )
        db.commit()
        return {
            **_empty_counts(),
            "failed": 1,
            "message": "Email polling lock unavailable",
        }
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()


def poll_email_messages(
    db,
    imap_factory=None,
    *,
    sleep=time.sleep,
    force_uids: set[int] | None = None,
) -> dict:
    """Fetch new UIDs read-only, committing each parent before its checkpoint."""
    started_at = time.monotonic()
    host = os.getenv("IMAP_HOST", "").strip()
    username = os.getenv("IMAP_USER", "").strip()
    password = os.getenv("IMAP_PASSWORD", "")
    if not username or not password:
        return _poll_configuration_failure(db, IMAP_CONFIG_ERROR)
    if not host:
        return _poll_configuration_failure(db, IMAP_REACHABILITY_ERROR)

    factory = imap_factory or _imap_factory
    client = None
    last_exc = None
    for attempt in range(3):
        try:
            client = factory(host, int(os.getenv("IMAP_PORT", "993")))
            client.login(username, password)
            break
        except (OSError, imaplib.IMAP4.error, TimeoutError) as exc:
            last_exc = exc
            if client:
                with contextlib.suppress(Exception):
                    client.logout()
            client = None
            if attempt < 2:
                sleep(2**attempt)
    if client is None:
        return _poll_configuration_failure(
            db, _friendly_imap_error(last_exc or RuntimeError())
        )

    mailbox = username
    folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    counts = _empty_counts()
    state = None
    try:
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise IMAPConnectionError("Could not select IMAP folder")
        validity = _uidvalidity(client)
        state = (
            db.query(EmailPollState)
            .filter_by(mailbox=mailbox, folder=folder)
            .with_for_update()
            .first()
        )
        all_uids = _uid_search(client, "ALL")
        highest_uid = max(all_uids, default=0)
        due_retry_uids: set[int] = set()
        initial_cutoff = None
        # uidvalidity=0 is the placeholder created when IMAP was unavailable
        # before the mailbox could be inspected. Treat the next successful
        # connection as the real first run and establish a baseline checkpoint.
        initial_run = state is None or state.uidvalidity == 0

        max_messages = max(1, int(os.getenv("EMAIL_POLL_MAX_MESSAGES", "25")))
        if initial_run:
            if state is None:
                state = EmailPollState(
                    mailbox=mailbox,
                    folder=folder,
                    uidvalidity=validity,
                    last_uid=highest_uid,
                )
                db.add(state)
            else:
                state.uidvalidity = validity
                state.last_uid = highest_uid
            lookback_hours = max(
                0, int(os.getenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "0"))
            )
            if lookback_hours <= 0 or not all_uids:
                return _record_run(
                    db,
                    state,
                    counts,
                    message="Email polling initialized without backfill",
                    started_at=started_at,
                )
            initial_cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
            since = initial_cutoff.strftime("%d-%b-%Y")
            uids = _uid_search(client, f"SINCE {since}")
            if not uids:
                return _record_run(
                    db,
                    state,
                    counts,
                    message="Email polling initialized without matching lookback mail",
                    started_at=started_at,
                )
            state.last_uid = max(0, min(uids) - 1)
            uids = uids[:max_messages]
            db.commit()
        elif state.uidvalidity != validity:
            state.uidvalidity = validity
            state.last_uid = 0
            db.commit()
            _audit(
                db,
                "email_poll_run",
                "Email UIDVALIDITY changed; checkpoint reset",
                detail="Message-ID deduplication protects replayed messages",
            )
            db.commit()
            uids = _uid_search(client, "UID 1:*")
            uids = uids[:max_messages]
        else:
            now = datetime.now(UTC)
            retry_rows = (
                db.query(EmailMessage)
                .filter_by(
                    mailbox=mailbox,
                    folder=folder,
                    uidvalidity=validity,
                    status="failed",
                )
                .all()
            )
            due_retry_uids = {
                row.uid
                for row in retry_rows
                if row.next_retry_at is None or row.next_retry_at <= now
            }
            due_retry_uids.update(force_uids or set())
            new_uids = _uid_search(client, f"UID {state.last_uid + 1}:*")
            new_uids = new_uids[:max_messages]
            uids = sorted(set(new_uids + list(due_retry_uids)))

        uids = [
            uid
            for uid in sorted(set(uids) | (force_uids or set()))
            if uid <= highest_uid
        ]
        counts["found"] = len(uids)
        blocked_by_failure = False

        for uid in uids:
            was_new_uid = uid > state.last_uid
            retry_record = (
                db.query(EmailMessage)
                .filter_by(
                    mailbox=mailbox,
                    folder=folder,
                    uidvalidity=validity,
                    uid=uid,
                )
                .first()
            )
            if (
                retry_record
                and retry_record.status == "failed"
                and retry_record.next_retry_at
                and retry_record.next_retry_at > datetime.now(UTC)
            ):
                if was_new_uid:
                    blocked_by_failure = True
                continue
            counts["emails"] += 1
            try:
                safe_to_advance = _process_uid(
                    db,
                    client,
                    mailbox,
                    folder,
                    uid,
                    counts,
                    uidvalidity=validity,
                    received_after=initial_cutoff if initial_run else None,
                )
            except Exception as exc:
                if _is_imap_connection_failure(exc):
                    raise IMAPConnectionError(_friendly_imap_error(exc)) from None
                db.rollback()
                _log_email_exception(uid, exc)
                safe_to_advance = _record_uid_fetch_failure(
                    db, mailbox, folder, uid, counts, exc, uidvalidity=validity
                )
            if was_new_uid and not safe_to_advance:
                blocked_by_failure = True
            if was_new_uid and safe_to_advance and not blocked_by_failure:
                state.last_uid = uid
                state.updated_at = datetime.now(UTC)
                db.commit()
            message_row = (
                db.query(EmailMessage)
                .filter_by(mailbox=mailbox, folder=folder, uid=uid)
                .first()
            )
            if message_row is not None:
                logger.debug(
                    "email_message %s",
                    json.dumps(
                        {
                            "message_id": message_row.message_id,
                            "sender": _sanitize_exception_message(
                                message_row.sender or ""
                            ),
                            "subject": _sanitize_exception_message(
                                message_row.subject or ""
                            ),
                            "outcome": message_row.status,
                            "reason": message_row.failure_reason
                            or message_row.filter_reason,
                        },
                        sort_keys=True,
                    ),
                )

        return _record_run(db, state, counts, started_at=started_at)
    except Exception as exc:
        friendly = _friendly_imap_error(exc)
        logger.warning("IMAP polling batch failed: %s", friendly)
        db.rollback()
        counts["failed"] += 1
        if state is None and mailbox:
            state = (
                db.query(EmailPollState)
                .filter_by(mailbox=mailbox, folder=folder)
                .first()
            )
        if state is not None:
            _record_run(
                db,
                state,
                counts,
                action="email_failed",
                message="Email polling batch failed",
                audit_result="error",
                audit_detail=friendly,
                started_at=started_at,
            )
        else:
            _audit(
                db,
                "email_failed",
                "Email polling batch failed",
                result="error",
                detail=friendly,
            )
            db.commit()
        return {**_count_result(counts), "message": friendly}
    finally:
        with contextlib.suppress(Exception):
            client.logout()


def _poll_configuration_failure(db, message: str) -> dict:
    logger.warning("Email polling unavailable: %s", message)
    mailbox = os.getenv("IMAP_USER", "").strip()
    folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    counts = {**_empty_counts(), "failed": 1}
    if mailbox:
        state = (
            db.query(EmailPollState).filter_by(mailbox=mailbox, folder=folder).first()
        )
        if state is None:
            state = EmailPollState(
                mailbox=mailbox,
                folder=folder,
                uidvalidity=0,
                last_uid=0,
            )
            db.add(state)
        _record_run(
            db,
            state,
            counts,
            action="email_failed",
            message="Email polling could not start",
            audit_result="error",
            audit_detail=message,
        )
    else:
        _audit(
            db,
            "email_failed",
            "Email polling could not start",
            result="error",
            detail=message,
        )
        db.commit()
    return {**_count_result(counts), "message": message}


def _imap_factory(host: str, port: int):
    if os.getenv("IMAP_USE_SSL", "true").lower() in {"1", "true", "yes", "on"}:
        return imaplib.IMAP4_SSL(host, port, timeout=30)
    return imaplib.IMAP4(host, port, timeout=30)


def _sender_filter_reason(
    msg: Message, mailbox: str, sender: str, db=None
) -> str | None:
    sender_key = sender.casefold()
    blocklist = _address_list("EMAIL_SENDER_BLOCKLIST")
    blocklist.update(_address_list("EMAIL_BLOCKED_SENDERS"))
    local, _, domain = sender_key.partition("@")
    if any(
        item == sender_key
        or item.lstrip("@.") == domain
        or item in local
        or item in sender_key
        for item in blocklist
    ):
        return "blocked_sender"
    subject = str(msg.get("Subject", "")).casefold()
    bounce_sender = (
        sender_key == "mailer-daemon"
        or sender_key == "postmaster"
        or sender_key.startswith("mailer-daemon@")
        or sender_key.startswith("postmaster@")
        or str(msg.get("Return-Path", "")).strip() == "<>"
    )
    bounce_subject = any(
        phrase in subject
        for phrase in (
            "delivery status notification",
            "undelivered mail",
            "returned mail",
            "failure notice",
            "mail delivery failed",
        )
    )
    if bounce_sender or bounce_subject:
        return "bounce"
    if _is_bulk_marketing(msg, sender_key):
        return "bulk_marketing"
    if _is_auto_reply(msg):
        return "auto_reply"
    if any(
        part.get_content_type().casefold() == "text/calendar"
        for part in msg.walk()
    ):
        return "calendar_invite"
    mailbox_key = email.utils.parseaddr(mailbox)[1].casefold() or mailbox.casefold()
    if sender_key and sender_key == mailbox_key:
        return "sender_is_mailbox"
    allowed = _address_list("EMAIL_ALLOWED_SENDERS")
    domains = _address_list("EMAIL_ALLOWED_DOMAINS")
    if (allowed or domains) and not (
        sender_key in allowed
        or _matches_domain_list(sender_key, allowed | domains)
        or _known_vendor_sender(
            db,
            sender_key,
            email.utils.parseaddr(str(msg.get("From", "")))[0],
        )
    ):
        return "sender_not_allowlisted"
    return None


def _is_bulk_marketing(msg: Message, sender: str) -> bool:
    return (
        msg.get("List-Unsubscribe") is not None
        or msg.get("Feedback-ID") is not None
        or bool(str(msg.get("X-Auto-Response-Suppress", "")).strip())
        or (
            msg.get("Auto-Submitted") is not None
            and str(msg.get("Auto-Submitted")).strip().casefold() != "no"
        )
        or str(msg.get("Precedence", "")).strip().casefold() in {"bulk", "list", "junk"}
        or _matches_domain_list(sender, _address_list("EMAIL_BULK_DOMAINS"))
    )


def _is_auto_reply(msg: Message) -> bool:
    return any(
        bool(str(msg.get(header, "")).strip())
        for header in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress")
    ) or (
        msg.get("Auto-Submitted") is not None
        and str(msg.get("Auto-Submitted")).strip().casefold() != "no"
    )


def _matches_domain_list(sender: str, entries: set[str]) -> bool:
    domain = sender.rpartition("@")[2]
    return any(
        domain == item.lstrip("@.") or domain.endswith("." + item.lstrip("@."))
        for item in entries
    )


def _known_vendor_sender(db, sender: str, sender_name: str = "") -> bool:
    allowed = _address_list("EMAIL_SENDER_ALLOWLIST")
    if sender in allowed or _matches_domain_list(sender, allowed):
        return True
    if db is None:
        return False
    domain = sender.rpartition("@")[2]
    normalized_sender_name = normalize_vendor_name(sender_name)
    for vendor in db.query(Vendor).all():
        contact = (getattr(vendor, "contact_email", None) or "").casefold()
        if contact == sender or (
            contact and _matches_domain_list(sender, {contact.rpartition("@")[2]})
        ):
            return True
    for alias in db.query(VendorAlias).all():
        value = (getattr(alias, "alias_norm", "") or "").casefold()
        if getattr(alias, "is_active", True) and (
            value in (sender, domain, normalized_sender_name)
            or _matches_domain_list(sender, {value})
        ):
            return True
    return False


def _address_list(variable: str) -> set[str]:
    default = (
        "no-reply,noreply,mailer-daemon,postmaster"
        if variable == "EMAIL_SENDER_BLOCKLIST"
        else ""
    )
    return {
        item.strip().casefold()
        for item in os.getenv(variable, default).split(",")
        if item.strip()
    }


def _safe_filename(filename: str, fallback_extension: str) -> str:
    name = PureWindowsPath(filename).name
    name = PurePath(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = f"attachment{fallback_extension}"
    if PurePath(name).suffix.lower() not in {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".docx",
    }:
        stem = PurePath(name).stem or "attachment"
        name = f"{stem}{fallback_extension}"
    return name[:180]


def _attachment_format(part: Message, payload: bytes) -> tuple[str, str] | None:
    content_type = part.get_content_type().casefold()
    filename = part.get_filename() or "attachment"
    if PurePath(PureWindowsPath(filename).name).suffix.casefold() == ".docx":
        if content_type != DOCX_MIME_TYPE or not is_valid_docx(
            filename, content_type, payload
        ):
            return None
        return ".docx", filename
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif payload.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif payload.find(b"%PDF-", 0, 1024 + len(b"%PDF-") - 1) >= 0:
        extension = ".pdf"
    else:
        return None
    expected_content_type = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }[extension]
    if content_type in ATTACHMENT_FORMATS and content_type not in {
        expected_content_type,
        "image/jpg" if extension == ".jpg" else expected_content_type,
    }:
        return None
    filename = filename if part.get_filename() else f"attachment{extension}"
    suffix = PurePath(PureWindowsPath(filename).name).suffix.casefold()
    expected_extensions = {".jpg", ".jpeg"} if extension == ".jpg" else {extension}
    if suffix and suffix not in expected_extensions:
        return None
    return extension, filename


def _record_failed_attachment(
    db, parent: Document, filename: str, content_type: str, payload: bytes, message: str
):
    digest = hashlib.sha256(payload).hexdigest()
    existing = db.query(Document).filter_by(file_hash=digest).first()
    if existing:
        return
    db.add(
        Document(
            id=uuid.uuid4(),
            original_filename=_safe_filename(filename, ".pdf"),
            file_hash=digest,
            sha256=digest,
            mime=content_type,
            size=len(payload),
            status="failed",
            failure_message=message[:500],
            parent_document_id=parent.id,
            source="email",
            email_sender=parent.email_sender,
            email_subject=parent.email_subject,
            email_received_at=parent.email_received_at,
            email_message_id=parent.email_message_id,
            email_uid=parent.email_uid,
            email_body_preview=parent.email_body_preview,
            email_eml_key=parent.email_eml_key,
        )
    )
    db.commit()


def _process_uid(
    db,
    client,
    mailbox: str,
    folder: str,
    uid: int,
    counts: dict,
    *,
    uidvalidity: int = 0,
    received_after: datetime | None = None,
) -> bool:
    """Return true only when this UID is committed or has exhausted retries."""
    try:
        status, response = client.uid("fetch", str(uid), "(INTERNALDATE BODY.PEEK[])")
    except Exception as exc:
        if _is_imap_connection_failure(exc):
            raise
        _log_email_exception(uid, exc)
        return _record_uid_fetch_failure(
            db, mailbox, folder, uid, counts, exc, uidvalidity=uidvalidity
        )
    if status != "OK" or not response:
        exc = TimeoutError("IMAP UID fetch returned a non-OK response")
        _log_email_exception(uid, exc)
        return _record_uid_fetch_failure(
            db, mailbox, folder, uid, counts, exc, uidvalidity=uidvalidity
        )
    raw = next((item[1] for item in response if isinstance(item, tuple)), None)
    if not isinstance(raw, bytes):
        exc = RuntimeError("IMAP UID had no message body")
        _log_email_exception(uid, exc)
        return _record_uid_fetch_failure(
            db, mailbox, folder, uid, counts, exc, uidvalidity=uidvalidity
        )

    server_received = _internal_date(response)
    msg, message_id, sender, subject, received, preview = _metadata(raw)
    received = server_received or received
    prior_by_id = db.query(EmailMessage).filter_by(message_id=message_id).first()
    prior_by_uid = (
        db.query(EmailMessage)
        .filter_by(
            mailbox=mailbox, folder=folder, uidvalidity=uidvalidity, uid=uid
        )
        .first()
    )
    prior = prior_by_id or prior_by_uid
    if prior and prior.status in {
        "processed",
        "ignored",
        "needs_attention",
        "failed_final",
    }:
        if prior.status != "failed_final":
            counts["duplicates"] += 1
            if not prior.filter_reason:
                prior.filter_reason = "Duplicate message ID"
            _audit(
                db,
                "email_skipped_duplicate",
                "Email was already ingested",
                detail=hashlib.sha256(message_id.encode()).hexdigest()[:16],
            )
        db.commit()
        return True

    attempt_no = (prior.attempts if prior else 0) + 1
    if prior is None:
        counts["new"] += 1
    date_for_key = received or datetime.now(UTC)
    message_hash = hashlib.sha1(message_id.encode()).hexdigest()
    eml_key = f"emails/{date_for_key:%Y/%m}/{message_hash}.eml"
    if prior is None:
        prior = EmailMessage(
            message_id=message_id,
            uid=uid,
            uidvalidity=uidvalidity,
            mailbox=mailbox,
            folder=folder,
            sender=sender or None,
            subject=subject or None,
            received_at=received,
            body_preview=preview,
            raw_object_key=None,
            status="processing",
            attempts=attempt_no,
        )
        db.add(prior)
    else:
        prior.message_id = message_id
        prior.uid = uid
        prior.uidvalidity = uidvalidity
        prior.folder = folder
        prior.sender = sender or None
        prior.subject = subject or None
        prior.received_at = received
        prior.body_preview = preview
        prior.raw_object_key = None
        prior.status = "processing"
        prior.attempts = attempt_no
        prior.next_retry_at = None
        prior.failure_reason = None
        prior.filter_reason = None
    db.commit()

    if received_after and (received is None or received < received_after):
        prior.status = "ignored"
        prior.failure_reason = "outside_initial_lookback"
        prior.filter_reason = "Outside configured initial lookback window"
        _audit(
            db,
            "email_skipped_filtered",
            "Message is older than the configured initial lookback",
            entity_id=prior.id,
            detail="outside_initial_lookback",
        )
        db.commit()
        _count_filtered(counts, "outside_initial_lookback")
        return True

    filter_reason = _sender_filter_reason(msg, mailbox, sender, db)
    if filter_reason:
        prior.status = "ignored"
        prior.failure_reason = _friendly_filter_reason(filter_reason)
        prior.filter_reason = _friendly_filter_reason(filter_reason)
        _audit(
            db,
            "email_skipped_filtered",
            "Email was skipped by configured sender or automation filters",
            entity_id=prior.id,
            detail=filter_reason,
        )
        db.commit()
        _count_filtered(counts, filter_reason)
        return True

    # Replies to a sent draft without a bill attachment are conversation events,
    # not new bill documents. Attachments still use the regular intake path.
    from src.services.email_draft_services import match_reply, record_vendor_reply

    source_flag = None
    in_reply_to = str(msg.get("In-Reply-To", "")).strip() or None
    reply_draft = match_reply(db, subject, in_reply_to)
    if reply_draft is not None and not source_flag and not _has_valid_attachment(msg):
        record_vendor_reply(
            db, reply_draft, sender=sender, subject=subject, message_id=message_id
        )
        prior.status = "processed"
        _audit(
            db,
            "vendor_replied",
            "Vendor replied to an email draft",
            entity_id=prior.id,
            detail=hashlib.sha256(message_id.encode()).hexdigest()[:16],
        )
        db.commit()
        counts["emails"] += 1
        return True

    if not _has_attachment_candidate(msg):
        if (
            os.getenv("EMAIL_BODY_INVOICE_ENABLED", "false").casefold()
            in {"1", "true", "yes", "on"}
            and _is_invoice_like_body(preview)
        ):
            date_for_key = received or datetime.now(UTC)
            body_message_key = hashlib.sha1(message_id.encode()).hexdigest()
            eml_key = f"emails/{date_for_key:%Y/%m}/{body_message_key}.eml"
            root_result = ingest_file(
                db,
                "email.eml",
                raw,
                "message/rfc822",
                None,
                "email",
                email_meta={
                    "email_sender": sender or None,
                    "email_subject": subject or None,
                    "email_received_at": received,
                    "email_message_id": message_id,
                    "email_uid": uid,
                    "email_body_preview": preview,
                    "email_eml_key": eml_key,
                },
                object_key=eml_key,
            )
            if root_result.document_id is None:
                raise RuntimeError("Raw email document could not be persisted")
            root = db.get(Document, root_result.document_id)
            if root is None:
                raise RuntimeError("Raw email document was unavailable after commit")
            body_result = ingest_file(
                db,
                "email-body.txt",
                preview.encode("utf-8"),
                "text/plain",
                None,
                "email",
                parent_document_id=root.id,
                email_meta={
                    "email_sender": sender or None,
                    "email_subject": subject or None,
                    "email_received_at": received,
                    "email_message_id": message_id,
                    "email_uid": uid,
                    "email_body_preview": preview,
                    "email_eml_key": eml_key,
                },
                internal_email_body=True,
            )
            if body_result.status != "received" or body_result.document_id is None:
                raise RuntimeError("Email body could not be queued for extraction")
            root.status = "processed"
            prior.status = "processed"
            prior.raw_object_key = eml_key
            counts["documents_created"] += 1
            db.commit()
            _audit(
                db,
                "email_body_ingested",
                "Invoice-like email body queued for experimental extraction",
                entity_id=prior.id,
            )
            db.commit()
            return True
        has_attachment_candidate = _has_raw_attachment_candidate(msg)
        reason = (
            "Only tiny inline images found"
            if has_attachment_candidate
            else "No attachment found"
        )
        prior.status = "ignored" if has_attachment_candidate else "needs_attention"
        prior.raw_object_key = None
        prior.filter_reason = reason
        prior.failure_reason = reason
        _audit(
            db,
            "email_skipped_filtered",
            "Email has no invoice attachment",
            entity_id=prior.id,
            detail=reason,
        )
        db.commit()
        _count_filtered(counts, reason.casefold().replace(" ", "_"))
        return True

    if not _has_valid_attachment(msg):
        reason = _friendly_filter_reason(_invalid_attachment_reason(msg))
        prior.status = (
            "needs_attention"
            if reason == "Attachment exceeds the configured size limit"
            else "ignored"
        )
        prior.failure_reason = reason
        prior.filter_reason = reason
        _audit(
            db,
            "email_skipped_filtered",
            "Email has no supported invoice attachment",
            entity_id=prior.id,
            detail=reason,
        )
        db.commit()
        _count_filtered(counts, reason.casefold().replace(" ", "_"))
        return True

    source_flag = None

    try:
        root = (
            db.query(Document)
            .filter_by(email_message_id=message_id, parent_document_id=None)
            .first()
        )
        if root is None:
            result = ingest_file(
                db,
                "email.eml",
                raw,
                "message/rfc822",
                None,
                "email",
                email_meta={
                    "email_sender": sender or None,
                    "email_subject": subject or None,
                    "email_received_at": received,
                    "email_message_id": message_id,
                    "email_uid": uid,
                    "email_body_preview": preview,
                    "email_eml_key": eml_key,
                    "source_flag": source_flag,
                },
                object_key=eml_key,
            )
            if (
                result.status not in {"received", "duplicate_blocked"}
                or result.document_id is None
            ):
                raise RuntimeError(
                    f"Raw email document could not be persisted: {result.message}"
                )
            root = db.get(Document, result.document_id)
        if root is None:
            raise RuntimeError("Raw email document was unavailable after commit")
        root.source_flag = source_flag
        prior.raw_object_key = eml_key
        db.commit()

        _audit(
            db,
            "email_received",
            "Email received for processing",
            entity_id=prior.id,
            detail=hashlib.sha256(message_id.encode()).hexdigest()[:16],
        )
        db.commit()
        if reply_draft is not None and not source_flag:
            record_vendor_reply(
                db,
                reply_draft,
                sender=sender,
                subject=subject,
                message_id=message_id,
                document_id=root.id,
            )
            db.commit()
        valid = 0
        attachment_failure = False
        accepted_attachment_count = 0
        attachment_reasons = []
        max_bytes = (
            max(1, int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "20"))) * 1024 * 1024
        )
        received_at = received or datetime.now(UTC)

        referenced_cids = _referenced_content_ids(msg)
        for part in msg.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename()
            disposition = part.get_content_disposition()
            is_candidate = bool(filename) or disposition in {"attachment", "inline"}
            if not is_candidate:
                continue
            payload = part.get_payload(decode=True) or b""
            content_type = part.get_content_type().casefold()
            if _skip_image_part(part, payload, referenced_cids):
                continue
            if source_flag and content_type != "application/pdf":
                attachment_failure = True
                attachment_reasons.append(
                    "Unsupported attachment type for filtered sender"
                )
                prior.failure_reason = attachment_reasons[-1]
                continue
            if accepted_attachment_count >= MAX_ATTACHMENTS_PER_EMAIL:
                counts["failed"] += 1
                attachment_failure = True
                attachment_reasons.append(
                    "Maximum of 10 attachments per email exceeded"
                )
                prior.failure_reason = attachment_reasons[-1]
                _audit(
                    db,
                    "email_failed",
                    "Attachment limit exceeded",
                    result="error",
                    entity_id=prior.id,
                    detail="More than 10 attachments",
                )
                continue
            accepted_attachment_count += 1
            original_name = filename or "attachment"
            if len(payload) > max_bytes:
                message = (
                    f"Attachment exceeds the {max_bytes // (1024 * 1024)} MB limit"
                )
                _record_failed_attachment(
                    db, root, original_name, content_type, payload, message
                )
                counts["failed"] += 1
                attachment_failure = True
                attachment_reasons.append(message)
                _audit(
                    db,
                    "email_failed",
                    "Email attachment exceeded the configured size limit",
                    result="error",
                    entity_id=prior.id,
                    detail=message,
                )
                continue
            detected = _attachment_format(part, payload)
            if detected is None:
                message = "Unsupported attachment type or file signature"
                _record_failed_attachment(
                    db,
                    root,
                    original_name,
                    content_type,
                    payload,
                    message,
                )
                counts["failed"] += 1
                attachment_failure = True
                attachment_reasons.append(message)
                _audit(
                    db,
                    "email_failed",
                    "Email attachment type or content did not match",
                    result="error",
                    entity_id=prior.id,
                    detail=message,
                )
                continue

            extension, _ = detected
            safe_name = _safe_filename(original_name, extension)
            # Normalize a missing extension to the verified media format.
            suffix = PurePath(safe_name).suffix.lower()
            if not suffix:
                safe_name = f"{safe_name}{extension}"
            digest = hashlib.sha256(payload).hexdigest()
            duplicate = db.query(Document).filter_by(file_hash=digest).first()
            if duplicate:
                counts["duplicates"] += 1
                valid += 1
                continue
            document_id = uuid.uuid4()
            object_key = f"incoming/{received_at:%Y/%m}/{document_id}/{safe_name}"
            result = ingest_file(
                db,
                safe_name,
                payload,
                content_type,
                None,
                "email",
                parent_document_id=root.id,
                email_meta={
                    "email_sender": sender or None,
                    "email_subject": subject or None,
                    "email_received_at": received,
                    "email_message_id": message_id,
                    "email_uid": uid,
                    "email_body_preview": preview,
                    "email_eml_key": eml_key,
                },
                object_key=object_key,
                document_id=document_id,
                source_flag=source_flag,
            )
            if result.status == "received":
                valid += 1
                counts["attachments"] += 1
                counts["documents_created"] += 1
                _audit(
                    db,
                    "attachment_ingested",
                    "Email attachment ingested",
                    entity_id=result.document_id,
                    detail=digest[:16],
                )
                db.commit()
            elif result.status == "duplicate_blocked":
                counts["duplicates"] += 1
                valid += 1
                prior.filter_reason = "Duplicate attachment content"
            else:
                attachment_failure = True

        if not valid:
            root.status = "ignored"
            root.failure_message = "No bill attachment found"
            prior.failure_reason = "No supported bill attachment found"
            prior.filter_reason = "No supported bill attachment found"
            _audit(
                db,
                "email_ignored",
                "No bill attachment found",
                entity_id=prior.id,
            )
            if not source_flag and not filter_reason:
                _queue_no_attachment_notification(db, root, sender)
        prior.status = "processed" if valid else "ignored"
        if valid:
            root.status = "processed"
            root.failure_message = None
        db.commit()
        if not valid:
            _dispatch_queued_notifications(db, root.id)
        if attachment_failure:
            return _record_email_failure(
                db,
                prior,
                uid,
                counts,
                RuntimeError(
                    "; ".join(attachment_reasons)
                    or "Email attachment ingestion returned an unsuccessful result"
                ),
            )
        return True
    except Exception as exc:
        _log_email_exception(uid, exc)
        db.rollback()
        retry_row = db.query(EmailMessage).filter_by(message_id=message_id).first()
        if retry_row is None:
            retry_row = EmailMessage(
                message_id=message_id,
                uid=uid,
                mailbox=mailbox,
                folder=folder,
                raw_object_key=eml_key,
                status="failed",
                attempts=attempt_no,
            )
            db.add(retry_row)
        retry_row.attempts = attempt_no
        retry_row.raw_object_key = eml_key if retry_row.raw_object_key else None
        return _record_email_failure(db, retry_row, uid, counts, exc)


def _has_valid_attachment(msg: Message, *, pdf_only: bool = False) -> bool:
    max_bytes = max(1, int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "20"))) * 1024 * 1024
    referenced_cids = _referenced_content_ids(msg)
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        if not (part.get_filename() or disposition in {"attachment", "inline"}):
            continue
        payload = part.get_payload(decode=True) or b""
        if _skip_image_part(part, payload, referenced_cids):
            continue
        if pdf_only and part.get_content_type().casefold() != "application/pdf":
            continue
        if len(payload) <= max_bytes and _attachment_format(part, payload):
            return True
    return False


def _has_attachment_candidate(msg: Message) -> bool:
    referenced_cids = _referenced_content_ids(msg)
    for part in msg.walk():
        if part.is_multipart():
            continue
        if not (
            part.get_filename()
            or part.get_content_disposition() in {"attachment", "inline"}
        ):
            continue
        payload = part.get_payload(decode=True) or b""
        if not _skip_image_part(part, payload, referenced_cids):
            return True
    return False


def _is_invoice_like_body(body: str) -> bool:
    return bool(
        re.search(r"\binvoice\b", body, re.IGNORECASE)
        and re.search(
            r"\b(total|amount due|invoice number|invoice no\.?|balance due)\b",
            body,
            re.IGNORECASE,
        )
    )


def _has_raw_attachment_candidate(msg: Message) -> bool:
    return any(
        not part.is_multipart()
        and (
            part.get_filename()
            or part.get_content_disposition() in {"attachment", "inline"}
        )
        for part in msg.walk()
    )


def _friendly_filter_reason(reason: str) -> str:
    labels = {
        "blocked_sender": "Sender is blocked by mailbox filters",
        "bulk_marketing": "Bulk or marketing email was skipped",
        "bounce": "Mail delivery failure message was skipped",
        "sender_not_allowlisted": "Sender is not on the allowed sender list",
        "outside_initial_lookback": "Outside configured initial lookback",
        "auto_reply": "Automated reply was skipped",
        "calendar_invite": "Calendar invitation was skipped",
        "no_attachment_found": "No attachment found",
        "only_tiny_inline_images_found": "Only tiny inline images found",
        "invalid_attachment": "Unsupported attachment type or file signature",
        "attachment_too_large": "Attachment exceeds the configured size limit",
    }
    return labels.get(reason, reason.replace("_", " ").capitalize())


def _has_valid_pdf_attachment(msg: Message) -> bool:
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        if not (part.get_filename() or disposition in {"attachment", "inline"}):
            continue
        payload = part.get_payload(decode=True) or b""
        detected = _attachment_format(part, payload)
        if detected and detected[0] == ".pdf":
            return True
    return False


def _referenced_content_ids(msg: Message) -> set[str]:
    html_parts = []
    for part in msg.walk():
        if part.get_content_type().casefold() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                html_parts.append(payload.decode(charset, errors="replace"))
    return {
        value.strip("<>\"' ").casefold()
        for value in re.findall(r"(?i)cid:([^\s\"'<>]+)", " ".join(html_parts))
    }


def _skip_image_part(part: Message, payload: bytes, referenced_cids: set[str]) -> bool:
    if not part.get_content_type().casefold().startswith("image/"):
        return False
    disposition = part.get_content_disposition()
    is_inline = disposition == "inline" or (
        disposition is None and not part.get_filename()
    )
    if not is_inline and (
        str(part.get("Content-ID", "")).strip("<> ").casefold()
        not in referenced_cids
    ):
        return False
    min_bytes = max(
        0, int(os.getenv("EMAIL_ATTACHMENT_MIN_BYTES", str(INLINE_IMAGE_MIN_BYTES)))
    )
    return bool(
        len(payload) < min_bytes
        or _is_tiny_image(payload, part.get_content_type().casefold())
    )


def _is_tiny_image(payload: bytes, content_type: str) -> bool:
    if (
        content_type == "image/png"
        and len(payload) >= 24
        and payload.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        width = int.from_bytes(payload[16:20], "big")
        height = int.from_bytes(payload[20:24], "big")
        return width < 100 or height < 100
    if content_type == "image/jpeg" and payload.startswith(b"\xff\xd8"):
        index = 2
        while index + 4 < len(payload):
            if payload[index] != 0xFF:
                index += 1
                continue
            marker = payload[index + 1]
            index += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(payload):
                break
            segment_length = int.from_bytes(payload[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(payload):
                break
            if (
                marker
                in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }
                and segment_length >= 7
            ):
                height = int.from_bytes(payload[index + 3 : index + 5], "big")
                width = int.from_bytes(payload[index + 5 : index + 7], "big")
                return width < 100 or height < 100
            index += segment_length
    return False


def _invalid_attachment_reason(msg: Message) -> str:
    max_bytes = max(1, int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "20"))) * 1024 * 1024
    found_candidate = False
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        if not (part.get_filename() or disposition in {"attachment", "inline"}):
            continue
        found_candidate = True
        payload = part.get_payload(decode=True) or b""
        if (
            disposition == "inline"
            and part.get_content_type().casefold().startswith("image/")
            and _is_tiny_image(payload, part.get_content_type().casefold())
        ):
            return "Only an inline image found, send a PDF"
        if len(payload) > max_bytes:
            return "attachment_too_large"
    return "invalid_attachment" if found_candidate else "no_valid_attachment"


def _queue_no_attachment_notification(db, root: Document, sender: str) -> None:
    if not sender or _is_automated_sender(sender):
        return
    allowed = _address_list("TEST_MAILBOX_ALLOWLIST")
    if sender.casefold() not in allowed:
        return
    existing = (
        db.query(Notification)
        .filter_by(
            document_id=root.id,
            recipient_email=sender,
            subject="No bill attachment found",
        )
        .first()
    )
    if existing:
        return
    db.add(
        Notification(
            document_id=root.id,
            recipient_email=sender,
            subject="No bill attachment found",
            body=(
                "No bill attachment found. Please resend with a PDF, PNG, or "
                "JPEG attachment."
            ),
            status="queued",
        )
    )
    db.commit()


def _is_automated_sender(sender: str) -> bool:
    return is_automated_sender(sender)


def _dispatch_queued_notifications(db, document_id) -> None:
    from src.worker.dispatch import dispatch_queued_notifications

    dispatch_queued_notifications(db, document_id)


def _record_uid_fetch_failure(
    db,
    mailbox: str,
    folder: str,
    uid: int,
    counts: dict,
    exc: Exception,
    *,
    uidvalidity: int = 0,
) -> bool:
    """Persist fetch attempts by mailbox/folder/UID without exposing IMAP errors."""
    row = (
        db.query(EmailMessage)
        .filter_by(
            mailbox=mailbox, folder=folder, uidvalidity=uidvalidity, uid=uid
        )
        .first()
    )
    attempt_no = (row.attempts if row else 0) + 1
    if row is None:
        key_digest = hashlib.sha1(f"{mailbox}:{folder}:{uid}".encode()).hexdigest()
        row = EmailMessage(
            message_id=f"<imap-fetch-{key_digest}@local>",
            uid=uid,
            uidvalidity=uidvalidity,
            mailbox=mailbox,
            folder=folder,
            raw_object_key=None,
            status="failed",
            attempts=attempt_no,
        )
        db.add(row)
    else:
        row.attempts = attempt_no
    row.attempts = attempt_no
    return _record_email_failure(db, row, uid, counts, exc)
