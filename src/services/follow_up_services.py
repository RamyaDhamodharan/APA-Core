"""Idempotent reminder scheduling and delivery for invoices and issues."""

import os
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.follow_up_models import FollowUp
from src.models.invoice_issue_models import InvoiceIssue, IssueEvent
from src.models.invoice_models import Invoice
from src.models.notification_models import Notification
from src.models.user_models import User
from src.models.vendor_models import Vendor
from src.services.email_draft_services import allowed_recipients, create_issue_draft
from src.services.invoice_issue_services import ACTIVE, add_issue_event
from src.services.invoice_state_services import transition_invoice

INVOICE_KINDS = {
    "verify_pending": ("verifying", "VERIFY_PENDING_DAYS", 1),
    "approval_pending": ("match_approved", "APPROVAL_PENDING_DAYS", 2),
    "overdue": ("overdue", "OVERDUE_REMINDER_DAYS", 1),
    "rejected_not_resubmitted": ("rejected", "REJECTED_REMINDER_DAYS", 7),
    "on_hold_stale": ("on_hold", "HOLD_STALE_DAYS", 5),
}
ISSUE_DRAFT_KIND = {
    "price_variance": "price_variance",
    "quantity_mismatch": "quantity_mismatch",
    "missing_receipt": "missing_receipt",
    "missing_po": "missing_po",
    "unknown_vendor": "unknown_vendor",
    "tax_mismatch": "tax_mismatch",
    "currency_mismatch": "currency_mismatch",
    "duplicate_invoice": "duplicate_invoice",
}


def _timezone():
    try:
        return ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _days(env_key: str, default: int) -> int:
    return max(0, int(os.getenv(env_key, str(default))))


def _ensure_followup(db, entity_type, entity_id, kind, recipient, due):
    active = (
        db.query(FollowUp)
        .filter(
            FollowUp.entity_type == entity_type,
            FollowUp.entity_id == entity_id,
            FollowUp.kind == kind,
            FollowUp.stopped_at.is_(None),
        )
        .first()
    )
    if active is not None:
        return None
    row = FollowUp(
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        recipient=recipient,
        next_due_at=due,
    )
    db.add(row)
    db.flush()
    return row


def _stop(row, reason, now):
    if row.stopped_at is None:
        row.stopped_at = now
        row.stop_reason = reason


def _audit(db, row, action, old, new, message):
    db.add(
        AuditLog(
            action=action,
            actor="system",
            entity_type="follow_up",
            entity_id=row.id,
            old_value=old,
            new_value=new,
            result="success",
            message=message,
        )
    )


def _owner_email(db, owner_id):
    owner = db.get(User, owner_id) if owner_id else None
    return owner.email if owner and owner.is_active else None


def _vendor_wait_started_at(db, issue):
    event = (
        db.query(IssueEvent)
        .filter(
            IssueEvent.issue_id == issue.id,
            IssueEvent.event_type == "status_changed",
        )
        .order_by(IssueEvent.created_at.desc())
        .first()
    )
    return event.created_at if event else (issue.created_at or datetime.now(UTC))


def schedule_follow_ups(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    today = now.astimezone(_timezone()).date()
    count = 0
    invoices = db.query(Invoice).filter(Invoice.is_current.is_(True)).all()
    for invoice in invoices:
        created = invoice.created_at or now
        recipient = _owner_email(db, invoice.owner_user_id)
        for kind, (status, env_key, default_days) in INVOICE_KINDS.items():
            if invoice.workflow_status == status and invoice.workflow_status != "paid":
                due = created + timedelta(days=_days(env_key, default_days))
                if kind == "overdue":
                    due = now
                if _ensure_followup(db, "invoice", invoice.id, kind, recipient, due):
                    count += 1
        if (
            invoice.due_date
            and today
            <= invoice.due_date
            <= today + timedelta(days=_days("DUE_SOON_DAYS", 3))
            and invoice.workflow_status not in {"paid", "rejected"}
        ):
            local_due = datetime.combine(invoice.due_date, time.min, tzinfo=_timezone())
            reminder_at = local_due.astimezone(UTC) - timedelta(
                days=_days("DUE_SOON_DAYS", 3)
            )
            if _ensure_followup(
                db,
                "invoice",
                invoice.id,
                "due_soon",
                recipient,
                max(now, reminder_at),
            ):
                count += 1

    issues = db.query(InvoiceIssue).filter(InvoiceIssue.status.in_(ACTIVE)).all()
    for issue in issues:
        invoice = db.get(Invoice, issue.invoice_id)
        recipient = _owner_email(db, issue.owner_user_id)
        if issue.owner_user_id is None and _ensure_followup(
            db,
            "issue",
            issue.id,
            "issue_unassigned",
            "unassigned",
            issue.created_at or now,
        ):
            count += 1
        if issue.status == "waiting_vendor" and _ensure_followup(
            db,
            "issue",
            issue.id,
            "issue_waiting_vendor",
            recipient,
            _vendor_wait_started_at(db, issue)
            + timedelta(days=_days("VENDOR_REPLY_REMINDER_DAYS", 3)),
        ):
            count += 1
    return count


def _stopped_reason(db, row):
    if row.entity_type == "invoice":
        entity = db.get(Invoice, row.entity_id)
        if entity is None:
            return "invoice deleted"
        if entity.workflow_status == "paid":
            return "invoice paid"
        if entity.workflow_status == "resubmitted":
            return "invoice resubmitted"
        if entity.workflow_status == "approved" and row.kind != "due_soon":
            return "invoice approved"
        if (
            row.kind == "due_soon"
            and entity.due_date < datetime.now(_timezone()).date()
        ):
            return "due date reached"
        if row.kind == "overdue" and entity.workflow_status != "overdue":
            return "invoice no longer overdue"
        if (
            row.kind == "rejected_not_resubmitted"
            and entity.workflow_status != "rejected"
        ):
            return "invoice resubmitted or status changed"
        if row.kind == "verify_pending" and entity.workflow_status != "verifying":
            return "verification completed"
        if row.kind == "on_hold_stale" and entity.workflow_status != "on_hold":
            return "hold released or status changed"
        current_email = _owner_email(db, entity.owner_user_id)
        if row.recipient == "unassigned" and current_email:
            return "invoice assigned"
        if (
            row.recipient
            and row.recipient != "unassigned"
            and current_email != row.recipient
        ):
            return "invoice reassigned"
    else:
        entity = db.get(InvoiceIssue, row.entity_id)
        if entity is None or entity.status in {"resolved", "dismissed"}:
            return "issue resolved or dismissed"
        current_email = _owner_email(db, entity.owner_user_id)
        if row.kind == "issue_unassigned" and entity.owner_user_id is not None:
            return "issue assigned"
        if row.recipient and current_email != row.recipient:
            return "issue reassigned"
        if row.kind == "issue_waiting_vendor" and entity.status != "waiting_vendor":
            return "vendor response received"
    return None


def _internal_recipients(db, row, entity):
    email = _owner_email(db, getattr(entity, "owner_user_id", None))
    if email:
        return [email] if email.casefold() in allowed_recipients() else []
    users = (
        db.query(User).filter(User.role == "associate", User.is_active.is_(True)).all()
    )
    return [
        user.email for user in users if user.email.casefold() in allowed_recipients()
    ]


def _queue_internal(db, row, entity, subject, body, now):
    invoice = (
        entity if isinstance(entity, Invoice) else db.get(Invoice, entity.invoice_id)
    )
    if invoice is None or not invoice.source_document_id:
        return []
    recipients = _internal_recipients(db, row, entity)
    notification_ids = []
    for recipient in recipients:
        notification = Notification(
            document_id=invoice.source_document_id,
            recipient_email=recipient,
            subject=f"AP internal reminder: {subject}",
            body=body,
            status="queued",
        )
        db.add(notification)
        db.flush()
        notification_ids.append(notification.id)
    return notification_ids


def _create_vendor_draft(db, row, issue, now):
    invoice = db.get(Invoice, issue.invoice_id)
    vendor = (
        db.get(Vendor, invoice.vendor_id) if invoice and invoice.vendor_id else None
    )
    document = (
        db.get(Document, invoice.source_document_id)
        if invoice and invoice.source_document_id
        else None
    )
    recipient = (
        vendor.contact_email
        if vendor and vendor.contact_email
        else document.email_sender
        if document
        else None
    )
    facts = {
        "invoice_number": invoice.invoice_no if invoice else None,
        "vendor_name": vendor.name
        if vendor
        else invoice.vendor_name_raw
        if invoice
        else None,
        "po_number": invoice.po_number_raw if invoice else None,
        "issue_description": issue.description,
        "expected": issue.expected_value,
        "actual": issue.actual_value,
    }
    draft = create_issue_draft(
        db,
        issue_id=issue.id,
        kind=ISSUE_DRAFT_KIND.get(issue.issue_type, "clarification_request"),
        facts=facts,
        recipient=recipient,
    )
    add_issue_event(
        db, issue, "vendor_contacted", None, f"Follow-up draft {draft.id} created"
    )
    return draft


def run_follow_ups(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    schedule_follow_ups(db, now)
    due_rows = (
        db.query(FollowUp)
        .filter(FollowUp.stopped_at.is_(None), FollowUp.next_due_at <= now)
        .order_by(FollowUp.next_due_at, FollowUp.id)
        .with_for_update(skip_locked=True)
        .all()
    )
    sent = stopped = 0
    pending_dispatch = []
    cap = max(1, int(os.getenv("MAX_REMINDERS", "3")))
    for row in due_rows:
        reason = _stopped_reason(db, row)
        if reason:
            _stop(row, reason, now)
            stopped += 1
            continue
        old = {"attempts": row.attempts, "next_due_at": row.next_due_at.isoformat()}
        if row.kind == "issue_waiting_vendor":
            issue = db.get(InvoiceIssue, row.entity_id)
            escalate_after = _days("VENDOR_REPLY_ESCALATE_DAYS", 7)
            waiting_since = _vendor_wait_started_at(db, issue) if issue else now
            if issue and now >= waiting_since + timedelta(days=escalate_after):
                issue.status = "escalated"
                add_issue_event(db, issue, "escalated", None, "Vendor reply overdue")
                pending_dispatch.extend(
                    _queue_internal(
                        db, row, issue, "Vendor reply overdue", issue.description, now
                    )
                )
                row.attempts += 1
                row.last_sent_at = now
                _stop(row, "vendor reply escalation sent", now)
                _audit(
                    db,
                    row,
                    "follow_up_escalated",
                    old,
                    {"attempts": row.attempts},
                    "Vendor reply reminder escalated",
                )
            else:
                _create_vendor_draft(db, row, issue, now)
                row.attempts += 1
                row.last_sent_at = now
                row.next_due_at = waiting_since + timedelta(days=escalate_after)
                _audit(
                    db,
                    row,
                    "follow_up_vendor_draft_created",
                    old,
                    {"attempts": row.attempts},
                    "Vendor follow-up saved as a draft",
                )
            sent += 1
        else:
            entity = db.get(Invoice, row.entity_id)
            if entity:
                # Notification dispatch checks allowlisting again before SMTP.
                pending_dispatch.extend(
                    _queue_internal(
                        db,
                        row,
                        entity,
                        row.kind.replace("_", " "),
                        f"Reminder: {row.kind.replace('_', ' ')} needs attention.",
                        now,
                    )
                )
            row.attempts += 1
            row.last_sent_at = now
            interval = _days("OVERDUE_REMINDER_DAYS", 1) if row.kind == "overdue" else 7
            row.next_due_at = now + timedelta(days=max(1, interval))
            _audit(
                db,
                row,
                "follow_up_sent",
                old,
                {"attempts": row.attempts},
                "Internal follow-up queued",
            )
            sent += 1
        if row.attempts >= cap and row.stopped_at is None:
            _stop(row, "maximum reminder count reached", now)
    db.commit()
    _dispatch_notifications(pending_dispatch)
    return {"scheduled": len(due_rows), "sent": sent, "stopped": stopped}


def mark_overdue(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    today = now.astimezone(_timezone()).date()
    rows = (
        db.query(Invoice)
        .filter(
            Invoice.is_current.is_(True),
            Invoice.workflow_status == "approved",
            Invoice.final_status == "approved",
            Invoice.paid_at.is_(None),
            Invoice.due_date < today,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    changed = 0
    pending_dispatch = []
    for invoice in rows:
        transition_invoice(
            db, invoice, "overdue", reason="Due date passed without a recorded payment"
        )
        db.add(
            AuditLog(
                action="invoice_overdue",
                actor="system",
                entity_type="invoice",
                entity_id=invoice.id,
                old_value={"workflow_status": "approved"},
                new_value={
                    "workflow_status": "overdue",
                    "due_date": invoice.due_date.isoformat(),
                },
                result="warning",
                message="Approved invoice is overdue",
            )
        )
        row = FollowUp(
            entity_type="invoice",
            entity_id=invoice.id,
            kind="overdue",
            recipient=_owner_email(db, invoice.owner_user_id),
            attempts=0,
            next_due_at=now,
        )
        db.add(row)
        pending_dispatch.extend(
            _queue_internal(
                db,
                row,
                invoice,
                "Invoice overdue",
                f"Invoice {invoice.invoice_no or ''} is overdue.",
                now,
            )
        )
        changed += 1
    db.commit()
    _dispatch_notifications(pending_dispatch)
    return changed


def _dispatch_notifications(notification_ids):
    from src.worker.dispatch import dispatch_notification

    for notification_id in notification_ids:
        dispatch_notification(None, notification_id)
