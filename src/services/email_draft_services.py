"""
Application module for template-backed, human-reviewed vendor email outbox services.
Flow: generate draft email notifications for invoice discrepancies, enforce test mailbox allowlists, send via SMTP upon explicit user action, and record vendor reply threads.
Called by: follow_up_services, document_workflow_services, email_draft_routers.
Calls: email_models, invoice_models, purchase_order_models, vendor_models.
Tables: email_drafts, email_draft_events, audit_logs, invoices, purchase_orders, vendors, notifications.
"""
import email.utils
import html
import logging
import os
import smtplib
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage

from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_draft_models import EmailDraft, EmailDraftEvent
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder
from src.models.vendor_models import Vendor

logger = logging.getLogger(__name__)

# Supported business categories for vendor emails
SUPPORTED_KINDS = {
    "price_variance",
    "quantity_mismatch",
    "missing_receipt",
    "missing_po",
    "unknown_vendor",
    "tax_mismatch",
    "currency_mismatch",
    "duplicate_invoice",
    "wrong_gl_or_details",
    "rejection_notice",
    "clarification_request",
    "payment_status_reply",
    "no_attachment",
    "no_match",
}


def allowed_recipients() -> set[str]:
    """
    Return set of allowed recipient email addresses configured in TEST_MAILBOX_ALLOWLIST.
    Inputs: none.
    Output: set[str] - lowercase recipient emails allowed to receive outbound messages.
    Effects: reads environment variable TEST_MAILBOX_ALLOWLIST.
    Results: prevents accidental emails to real vendors in test or staging environments.
    """
    return {
        value.strip().casefold()
        for value in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
        if value.strip()
    }


def is_recipient_allowed(email: str | None) -> bool:
    if not email:
        return False
    allowed = allowed_recipients()
    if "*" in allowed:
        return True
    return email.strip().casefold() in allowed


def _money(value) -> str | None:
    """Format decimal amount as a 2-decimal place string."""
    return f"{value:.2f}" if value is not None else None


def _template(kind: str, facts: dict[str, object]) -> tuple[str, str]:
    """
    Generate professional email subject and body for a given discrepancy kind and invoice facts.
    Inputs: kind (str), facts (dict[str, object]).
    Output: tuple[str, str] - (subject, body).
    Effects: pure template interpolation.
    Results: formatted draft text for human review before transmission.
    """
    labels = {
        "price_variance": "Price variance clarification",
        "quantity_mismatch": "Quantity mismatch clarification",
        "missing_receipt": "Goods receipt clarification",
        "missing_po": "Purchase order clarification",
        "unknown_vendor": "Vendor details clarification",
        "tax_mismatch": "Tax amount clarification",
        "currency_mismatch": "Currency clarification",
        "duplicate_invoice": "Duplicate invoice clarification",
        "wrong_gl_or_details": "Invoice details clarification",
        "rejection_notice": "Invoice status notice",
        "clarification_request": "Invoice clarification request",
        "payment_status_reply": "Payment status response",
        "no_attachment": "Please resend your invoice",
        "no_match": "Invoice requires clarification",
    }
    subject = labels[kind]
    facts_line = "; ".join(
        f"{key}: {value}" for key, value in facts.items() if value not in (None, "")
    )
    details = facts_line or "No invoice details were available for this message."
    prompts = {
        "price_variance": (
            "Please confirm the price difference shown below and provide a corrected "
            "invoice or supporting explanation."
        ),
        "quantity_mismatch": (
            "Please confirm the billed quantity shown below and provide a corrected "
            "invoice or supporting delivery details."
        ),
        "missing_receipt": (
            "Please provide the goods receipt or delivery details for this invoice."
        ),
        "missing_po": "Please provide the purchase order reference for this invoice.",
        "unknown_vendor": (
            "Please confirm your registered vendor details so this invoice can be "
            "reviewed."
        ),
        "tax_mismatch": (
            "Please confirm the tax amounts shown below and provide a corrected "
            "invoice if needed."
        ),
        "currency_mismatch": (
            "Please confirm the invoice currency and provide a corrected invoice "
            "if needed."
        ),
        "duplicate_invoice": (
            "Please confirm whether this invoice is a replacement for a previously "
            "submitted invoice."
        ),
        "wrong_gl_or_details": (
            "Please confirm the invoice coding or details shown below."
        ),
        "rejection_notice": (
            "This invoice was rejected. Please review the details below and contact "
            "us if you believe this is incorrect."
        ),
        "clarification_request": (
            "Please review the invoice details below and provide the requested "
            "clarification."
        ),
        "payment_status_reply": (
            "Here is the payment status information currently recorded for this "
            "invoice."
        ),
        "no_attachment": (
            "We could not find a bill attachment in your email. Please resend it "
            "with a PDF, PNG, or JPEG bill attached."
        ),
        "no_match": (
            "We could not match this invoice to a known vendor or purchase order. "
            "Please confirm the details below."
        ),
    }
    body = (
        f"Hello {facts.get('vendor') or 'vendor'},\n\n{prompts[kind]}\n\n"
        f"{details}\n\nRegards,\nAccounts Payable"
    )
    return subject, body


def invoice_facts(db: Session, invoice: Invoice) -> dict[str, object]:
    """
    Extract factual details and line differences from an invoice and linked PO for email drafting.
    Inputs: db (Session), invoice (Invoice).
    Output: dict[str, object] - dictionary of invoice and PO comparison facts.
    Effects: reads database rows from invoices, purchase_orders, po_lines, vendors.
    Results: factual summary included in vendor email body.
    """
    # Step 1: Resolve header entities
    vendor = db.get(Vendor, invoice.vendor_id) if invoice.vendor_id else None
    po = db.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None
    facts: dict[str, object] = {
        "vendor": vendor.name if vendor else invoice.vendor_name_raw,
        "invoice": invoice.invoice_no,
        "PO": po.po_number if po else invoice.po_number_raw,
        "invoice total": _money(invoice.total_amount),
        "workflow status": getattr(invoice, "workflow_status", None),
    }
    if invoice.currency:
        facts["currency"] = invoice.currency.strip()
    if invoice.tax_amount is not None:
        facts["billed tax"] = _money(invoice.tax_amount)
    expected_tax = None

    # Step 2: Compare against PO if linked
    if po:
        if po.total_amount is not None:
            facts["PO total"] = _money(po.total_amount)
        if po.tax_amount is not None:
            expected_tax = Decimal(po.tax_amount)
        elif po.tax_rate is not None and po.subtotal is not None:
            expected_tax = Decimal(po.subtotal) * Decimal(po.tax_rate) / Decimal(100)
        if (
            invoice.currency
            and po.currency
            and invoice.currency.strip() != po.currency.strip()
        ):
            facts["PO currency"] = po.currency.strip()
        if invoice.total_amount is not None and po.total_amount is not None:
            facts["total difference"] = _money(invoice.total_amount - po.total_amount)

        # Batch query PO lines to eliminate N+1 query loop
        lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
        po_line_ids = [line.po_line_id for line in lines if line.po_line_id]
        if po_line_ids:
            from src.models.purchase_order_models import PurchaseOrderLine

            po_lines_by_id = {
                pl.id: pl
                for pl in db.query(PurchaseOrderLine)
                .filter(PurchaseOrderLine.id.in_(po_line_ids))
                .all()
            }
        else:
            po_lines_by_id = {}

        for line in lines:
            if line.po_line_id:
                po_line = po_lines_by_id.get(line.po_line_id)
                if po_line:
                    line_name = line.description or line.item_code or line.id
                    facts[f"line {line_name} expected qty"] = str(po_line.qty)
                    facts[f"line {line_name} billed qty"] = str(line.qty)
                    if line.qty is not None and po_line.qty is not None:
                        facts[f"line {line_name} qty difference"] = str(
                            Decimal(line.qty) - Decimal(po_line.qty)
                        )
                    facts[f"line {line_name} expected unit price"] = _money(
                        po_line.unit_price
                    )
                    facts[f"line {line_name} billed unit price"] = _money(
                        line.unit_price
                    )
                    if line.unit_price is not None and po_line.unit_price is not None:
                        facts[f"line {line_name} unit price difference"] = _money(
                            Decimal(line.unit_price) - Decimal(po_line.unit_price)
                        )
                    facts[f"line {line_name} expected amount"] = _money(
                        po_line.line_amount
                    )
                    facts[f"line {line_name} billed amount"] = _money(line.line_amount)
                    if line.line_amount is not None and po_line.line_amount is not None:
                        facts[f"line {line_name} amount difference"] = _money(
                            Decimal(line.line_amount) - Decimal(po_line.line_amount)
                        )
    if (
        expected_tax is None
        and vendor
        and getattr(vendor, "default_tax_rate", None) is not None
        and getattr(invoice, "subtotal", None) is not None
    ):
        expected_tax = (
            Decimal(invoice.subtotal) * Decimal(vendor.default_tax_rate) / Decimal(100)
        )
    if expected_tax is not None:
        facts["expected tax"] = _money(expected_tax)
        if invoice.tax_amount is not None:
            facts["tax difference"] = _money(Decimal(invoice.tax_amount) - expected_tax)
    if invoice.gl_code:
        facts["GL code"] = invoice.gl_code
    return facts


def _event(
    db,
    draft,
    event_type: str,
    user_id=None,
    old=None,
    new=None,
    *,
    document_id=None,
    message_id=None,
):
    """Record an audit trail event for draft lifecycle transitions."""
    db.add(
        EmailDraftEvent(
            draft_id=draft.id,
            event_type=event_type,
            user_id=user_id,
            old_value=old,
            new_value=new,
            document_id=document_id,
            message_id=message_id,
        )
    )
    db.add(
        AuditLog(
            action=f"email_draft_{event_type}",
            entity_type="email_draft",
            entity_id=draft.id,
            old_value=old,
            new_value=new,
            result="success",
            message=f"Email draft {event_type}",
            created_by=user_id,
        )
    )


def create_invoice_draft(
    db: Session, invoice: Invoice, kind: str, user_id=None
) -> EmailDraft:
    """
    Create a reviewable email draft for an invoice discrepancy.
    Inputs: db (Session), invoice (Invoice), kind (str), user_id (UUID | None).
    Output: EmailDraft - persisted draft in 'draft' status.
    Effects: inserts row into email_drafts and email_draft_events.
    Results: draft visible in frontend drawer for human review and transmission.
    """
    if kind not in SUPPORTED_KINDS:
        raise ValueError("Unsupported email draft kind")
    # Step 1: Assemble invoice comparison facts
    facts = invoice_facts(db, invoice)
    subject, body = _template(kind, facts)

    # Step 2: Determine vendor contact recipient
    vendor = db.get(Vendor, invoice.vendor_id) if invoice.vendor_id else None
    recipient = vendor.contact_email if vendor and vendor.contact_email else None
    if not recipient and invoice.source_document_id:
        doc = db.get(Document, invoice.source_document_id)
        recipient = doc.email_sender if doc else None
    recipients = [recipient] if recipient else []

    # Step 3: Construct reviewable draft record with thread tracking key
    ref = str(invoice.id).replace("-", "")[:8].upper()
    draft = EmailDraft(
        kind=kind,
        invoice_id=invoice.id,
        document_id=invoice.source_document_id,
        to_addresses=recipients,
        cc_addresses=[],
        subject=subject,
        body=body,
        status="draft",
        blocked_by_allowlist=not bool(recipients)
        or any(r.casefold() not in allowed_recipients() for r in recipients),
        created_by=user_id,
        generated_by="template",
        thread_key=f"[AP-{ref}]",
    )
    db.add(draft)
    db.flush()
    _event(
        db,
        draft,
        "created",
        user_id,
        new={
            "kind": kind,
            "to": recipients,
            "generated_by": "template",
            "blocked_by_allowlist": draft.blocked_by_allowlist,
        },
    )
    return draft


def create_no_match_draft(db: Session, invoice: Invoice, sender: str) -> EmailDraft:
    """
    Create a reviewable no-match email draft; never sent automatically during ingestion.
    Inputs: db (Session), invoice (Invoice), sender (str).
    Output: EmailDraft - persisted draft.
    Effects: writes to email_drafts.
    Results: allows reviewer to notify sender about missing PO or unknown vendor.
    """
    reason = "No purchase order / unknown vendor"
    recipient = sender.strip()
    recipients = [recipient] if recipient else []
    draft = EmailDraft(
        kind="no_match",
        invoice_id=invoice.id,
        document_id=invoice.source_document_id,
        to_addresses=recipients,
        cc_addresses=[],
        subject="Invoice needs attention",
        body=f"We could not match this invoice to a purchase order. {reason}.",
        status="draft",
        blocked_by_allowlist=not bool(recipients)
        or any(
            address.casefold() not in allowed_recipients()
            for address in recipients
        ),
        created_by=None,
        generated_by="template",
        thread_key=f"[AP-{str(invoice.id).replace('-', '')[:8].upper()}]",
    )
    db.add(draft)
    db.flush()
    _event(db, draft, "created", None, new={"kind": draft.kind, "to": recipients,
        "blocked_by_allowlist": draft.blocked_by_allowlist})
    return draft


def create_issue_draft(
    db: Session,
    *,
    issue_id: uuid.UUID,
    kind: str,
    facts: dict[str, object],
    recipient: str | None,
    user_id=None,
) -> EmailDraft:
    """Create a reviewable draft linked to an InvoiceIssue record."""
    if kind not in SUPPORTED_KINDS:
        raise ValueError("Unsupported email draft kind")
    subject, body = _template(kind, facts)
    recipients = [recipient] if recipient else []
    draft = EmailDraft(
        kind=kind,
        issue_id=issue_id,
        to_addresses=recipients,
        cc_addresses=[],
        subject=subject,
        body=body,
        blocked_by_allowlist=not bool(recipients)
        or any(r.casefold() not in allowed_recipients() for r in recipients),
        created_by=user_id,
        generated_by="template",
        thread_key=f"[AP-{str(issue_id).replace('-', '')[:8].upper()}]",
    )
    db.add(draft)
    db.flush()
    _event(
        db,
        draft,
        "created",
        user_id,
        new={
            "kind": kind,
            "to": recipients,
            "generated_by": "template",
            "blocked_by_allowlist": draft.blocked_by_allowlist,
        },
    )
    return draft


def _is_allowlisted(draft: EmailDraft) -> bool:
    """Check if all draft recipients are in the configured TEST_MAILBOX_ALLOWLIST."""
    return bool(draft.to_addresses) and all(
        is_recipient_allowed(item)
        for item in [*draft.to_addresses, *draft.cc_addresses]
    )


def _transient_smtp_failure(exc: Exception) -> bool:
    """Identify transient network or SMTP server errors eligible for retry."""
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(
        exc,
        (
            OSError,
            TimeoutError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
        ),
    )


def send_draft(
    db: Session, draft: EmailDraft, user_id=None, *, sleep=time.sleep
) -> bool:
    """
    Transmit an approved email draft over SMTP with retry backoff and state tracking.
    Inputs: db (Session), draft (EmailDraft), user_id (UUID | None), sleep (Callable).
    Output: bool - True if sent successfully, False otherwise.
    Effects: sends email over SMTP; updates draft status to 'sent' or 'failed'; records audit logs.
    Results: delivers clarification request to vendor.
    """
    # Step 1: Validate draft status
    if draft.status == "sent":
        raise ValueError("This email draft has already been sent")
    if draft.status == "discarded":
        raise ValueError("This email draft was discarded")

    # Step 2: Enforce test mailbox allowlist safety check
    if not _is_allowlisted(draft):
        draft.blocked_by_allowlist = True
        draft.last_error = "Recipients are not in TEST_MAILBOX_ALLOWLIST"
        return False
    draft.blocked_by_allowlist = False

    # Step 3: Construct MIME email message with thread tracking token
    short_ref = (
        draft.thread_key
        or "[AP-"
        + str(draft.invoice_id or draft.issue_id or draft.id)
        .replace("-", "")[:8]
        .upper()
        + "]"
    )
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or os.getenv("IMAP_USER") or "admin@genworx.ai"
    message["To"] = ", ".join(draft.to_addresses)
    if draft.cc_addresses:
        message["Cc"] = ", ".join(draft.cc_addresses)
    message["Subject"] = (
        draft.subject
        if short_ref.casefold() in draft.subject.casefold()
        else f"{draft.subject} {short_ref}"
    )
    message["Message-ID"] = draft.message_id or email.utils.make_msgid()
    if draft.message_id:
        message["In-Reply-To"] = draft.message_id
        message["References"] = draft.message_id
    message.set_content(draft.body)
    message.add_alternative(
        f"<html><body><pre>{html.escape(draft.body)}</pre></body></html>",
        subtype="html",
    )

    # Step 4: Transmit via SMTP with exponential backoff on transient errors
    max_attempts = max(1, int(os.getenv("EMAIL_DRAFT_MAX_ATTEMPTS", "3")))
    old_status = draft.status
    old_attempts = draft.attempts
    for attempt in range(max_attempts):
        draft.attempts += 1
        try:
            host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
            port = int(os.getenv("SMTP_PORT", "587"))
            if os.getenv("SMTP_USE_SSL", "false").casefold() in {"1", "true", "yes"}:
                smtp = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                smtp = smtplib.SMTP(host, port, timeout=20)
            with smtp:
                user = os.getenv("SMTP_USER") or os.getenv("IMAP_USER")
                password = os.getenv("SMTP_PASSWORD") or os.getenv("IMAP_PASSWORD")
                if user and hasattr(smtp, "login"):
                    if hasattr(smtp, "starttls"):
                        smtp.starttls()
                    smtp.login(user, password or "")
                smtp.send_message(message)

            # Step 5: Mark status sent and record audit log
            old = {"status": draft.status, "attempts": draft.attempts - 1}
            draft.status = "sent"
            draft.sent_by = user_id
            draft.sent_at = datetime.now(UTC)
            draft.message_id = message.get("Message-ID") or message["Message-ID"]
            draft.last_error = None
            _event(
                db,
                draft,
                "sent",
                user_id,
                old=old,
                new={"status": "sent", "message_id": draft.message_id},
            )
            if draft.invoice_id:
                db.add(
                    AuditLog(
                        action="outbound_email_sent",
                        entity_type="invoice",
                        entity_id=draft.invoice_id,
                        old_value=old,
                        new_value={"draft_id": str(draft.id), "kind": draft.kind},
                        result="success",
                        message="Vendor email sent",
                        created_by=user_id,
                    )
                )
            if getattr(draft, "issue_id", None):
                from src.models.invoice_issue_models import InvoiceIssue
                from src.services.invoice_issue_services import add_issue_event

                issue = db.get(InvoiceIssue, draft.issue_id)
                if issue is not None and issue.status in {
                    "open",
                    "in_progress",
                    "escalated",
                }:
                    issue.status = "waiting_vendor"
                    add_issue_event(
                        db,
                        issue,
                        "status_changed",
                        user_id,
                        "Vendor email draft sent; awaiting reply",
                    )
            return True
        except Exception as exc:
            logger.warning("Email draft SMTP attempt failed (%s)", type(exc).__name__)
            draft.last_error = "Email could not be sent. Check SMTP settings and retry."
            if _transient_smtp_failure(exc) and attempt + 1 < max_attempts:
                sleep(2**attempt)
                continue
            break

    # Step 6: Mark failed if all retries exhausted
    old = {"status": old_status, "attempts": old_attempts}
    draft.status = "failed"
    _event(
        db,
        draft,
        "failed",
        user_id,
        old=old,
        new={
            "status": "failed",
            "attempts": draft.attempts,
            "message": "Email could not be sent; retry the draft",
        },
    )
    return False


def match_reply(db: Session, subject: str, in_reply_to: str | None):
    """Find a sent draft by reference token in the subject or message linkage."""
    query = db.query(EmailDraft).filter(EmailDraft.status == "sent")
    predicates = []
    if in_reply_to:
        predicates.append(EmailDraft.message_id == in_reply_to.strip())
    if subject:
        predicates.append(EmailDraft.thread_key.is_not(None))
    if not predicates:
        return None
    for draft in query.order_by(EmailDraft.sent_at.desc()).all():
        if in_reply_to and draft.message_id and draft.message_id == in_reply_to.strip():
            return draft
        if draft.thread_key and draft.thread_key.casefold() in subject.casefold():
            return draft
    return None


def record_vendor_reply(
    db: Session,
    draft: EmailDraft,
    *,
    sender: str,
    subject: str,
    message_id: str,
    document_id=None,
):
    """Record an incoming email reply linked to a previous draft thread."""
    _event(
        db,
        draft,
        "vendor_replied",
        None,
        new={"sender": sender, "subject": subject},
        document_id=document_id,
        message_id=message_id,
    )
    if getattr(draft, "issue_id", None):
        from src.models.document_models import Document
        from src.models.invoice_issue_models import InvoiceIssue
        from src.models.invoice_models import Invoice
        from src.models.notification_models import Notification
        from src.models.user_models import User
        from src.services.invoice_issue_services import add_issue_event

        issue = db.get(InvoiceIssue, draft.issue_id)
        if issue is not None:
            if issue.status == "waiting_vendor":
                issue.status = "in_progress"
            add_issue_event(
                db, issue, "vendor_replied", None, f"Reply received from {sender}"
            )
            if issue.owner_user_id:
                owner = db.get(User, issue.owner_user_id)
                invoice = db.get(Invoice, issue.invoice_id)
                document = (
                    db.get(Document, invoice.source_document_id)
                    if invoice and invoice.source_document_id
                    else None
                )
                allowlist = {
                    item.strip().casefold()
                    for item in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
                    if item.strip()
                }
                if owner and owner.email.casefold() in allowlist and document:
                    db.add(
                        Notification(
                            document_id=document.id,
                            recipient_email=owner.email,
                            subject="Vendor replied to an invoice issue",
                            body=(
                                "A vendor reply was received for invoice "
                                f"{invoice.invoice_no or ''}."
                            ),
                            status="queued",
                        )
                    )


def ensure_notification_draft(db: Session, notification):
    """Mirror legacy queued notification rows into the common outbox."""
    draft = (
        db.query(EmailDraft)
        .filter_by(
            document_id=notification.document_id,
            to_addresses=[notification.recipient_email],
            subject=notification.subject or "",
        )
        .with_for_update()
        .first()
    )
    if draft is not None:
        return draft
    document = db.get(Document, notification.document_id)
    invoice = (
        db.query(Invoice).filter_by(source_document_id=notification.document_id).first()
    )
    kind = (
        "no_attachment"
        if notification.subject == "No bill attachment found"
        else "no_match"
        if notification.subject
        in {"Invoice needs attention", "Invoice could not be matched"}
        else "clarification_request"
    )
    token_id = invoice.id if invoice else document.id if document else uuid.uuid4()
    token = f"[AP-{str(token_id).replace('-', '')[:8].upper()}]"
    draft = EmailDraft(
        kind=kind,
        invoice_id=invoice.id if invoice else None,
        document_id=notification.document_id,
        to_addresses=[notification.recipient_email],
        cc_addresses=[],
        subject=notification.subject or "AP Assistant notification",
        body=notification.body or "",
        status="draft",
        blocked_by_allowlist=notification.recipient_email.casefold()
        not in allowed_recipients(),
        generated_by="template",
        thread_key=token,
    )
    db.add(draft)
    db.flush()
    _event(
        db,
        draft,
        "created",
        None,
        new={
            "kind": kind,
            "source": "notification_outbox",
            "blocked_by_allowlist": draft.blocked_by_allowlist,
        },
    )
    return draft
