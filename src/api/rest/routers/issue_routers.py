import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.follow_up_models import FollowUp
from src.models.invoice_issue_models import InvoiceIssue, IssueEvent
from src.models.invoice_models import Invoice
from src.models.notification_models import Notification
from src.models.user_models import User
from src.models.vendor_models import Vendor
from src.services.email_draft_services import create_issue_draft
from src.services.invoice_issue_services import ACTIVE, add_issue_event
from src.services.invoice_state_services import transition_invoice

router = APIRouter(prefix="/api", tags=["Invoice issues"])


def _uid(user):
    try:
        return uuid.UUID(str(user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _get(db, issue_id):
    issue = db.get(InvoiceIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


def _serialize(db, issue, with_events=False):
    invoice = db.get(Invoice, issue.invoice_id)
    data = {
        key: getattr(issue, key)
        for key in (
            "id",
            "invoice_id",
            "issue_type",
            "severity",
            "description",
            "expected_value",
            "actual_value",
            "status",
            "owner_user_id",
            "due_at",
            "created_at",
            "resolved_at",
            "resolution_note",
        )
    }
    data["invoice_no"] = invoice.invoice_no if invoice else None
    data["vendor_id"] = invoice.vendor_id if invoice else None
    if with_events:
        events = (
            db.query(IssueEvent)
            .filter_by(issue_id=issue.id)
            .order_by(IssueEvent.created_at, IssueEvent.id)
            .all()
        )
        data["events"] = [
            {
                key: getattr(event, key)
                for key in ("id", "event_type", "user_id", "note", "created_at")
            }
            for event in events
        ]
    return data


def _audit(db, issue, action, user_id, reason, old, new):
    db.add(
        AuditLog(
            action=action,
            entity_type="invoice_issue",
            entity_id=issue.id,
            old_value=old,
            new_value=new,
            reason=reason,
            result="success",
            message=action.replace("_", " ").title(),
            created_by=user_id,
        )
    )


class NoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=4000)

    @field_validator("note")
    @classmethod
    def note_not_blank(cls, value):
        if not value.strip():
            raise ValueError("A non-empty note is required")
        return value.strip()


class AssignRequest(BaseModel):
    owner_user_id: uuid.UUID | None = None


class ReassignRequest(BaseModel):
    owner_user_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()


class EscalateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()


class DraftRequest(BaseModel):
    kind: str = "clarification_request"


@router.get("/issues")
def list_issues(
    status: str | None = None,
    owner: uuid.UUID | None = None,
    vendor: uuid.UUID | None = None,
    type: str | None = None,
    invoice: uuid.UUID | None = None,
    overdue: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    query = db.query(InvoiceIssue).join(Invoice, InvoiceIssue.invoice_id == Invoice.id)
    if status:
        query = query.filter(InvoiceIssue.status == status)
    if owner is not None:
        query = query.filter(InvoiceIssue.owner_user_id == owner)
    if vendor is not None:
        query = query.filter(Invoice.vendor_id == vendor)
    if type:
        query = query.filter(InvoiceIssue.issue_type == type)
    if invoice is not None:
        query = query.filter(InvoiceIssue.invoice_id == invoice)
    if overdue is True:
        query = query.filter(
            InvoiceIssue.due_at < datetime.now(UTC), InvoiceIssue.status.in_(ACTIVE)
        )
    elif overdue is False:
        query = query.filter(
            (InvoiceIssue.due_at.is_(None))
            | (InvoiceIssue.due_at >= datetime.now(UTC))
            | InvoiceIssue.status.not_in(ACTIVE)
        )
    total = query.count()
    rows = (
        query.order_by(InvoiceIssue.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_serialize(db, row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/issues/{issue_id}")
def get_issue(
    issue_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    return _serialize(db, _get(db, issue_id), True)


@router.post("/issues/{issue_id}/comment")
def comment_issue(
    issue_id: uuid.UUID,
    request: NoteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    issue = _get(db, issue_id)
    uid = _uid(user)
    add_issue_event(db, issue, "comment", uid, request.note)
    db.commit()
    return _serialize(db, issue, True)


def _assign(issue, db, uid, target, reason, action):
    if target is None:
        target = issue.owner_user_id or uid
    if target is not None:
        user = db.get(User, target)
        if user is None or user.role != "associate" or not user.is_active:
            raise HTTPException(
                status_code=422, detail="Owner must be an active associate"
            )
    old = {"owner_user_id": str(issue.owner_user_id) if issue.owner_user_id else None}
    issue.owner_user_id = target
    if action == "reassigned":
        now = datetime.now(UTC)
        rows = (
            db.query(FollowUp)
            .filter(
                FollowUp.entity_type == "issue",
                FollowUp.entity_id == issue.id,
                FollowUp.stopped_at.is_(None),
            )
            .with_for_update()
            .all()
        )
        for row in rows:
            row.stopped_at = now
            row.stop_reason = "issue reassigned"
            db.add(
                AuditLog(
                    action="follow_up_stopped",
                    entity_type="follow_up",
                    entity_id=row.id,
                    old_value={"stopped_at": None},
                    new_value={"stop_reason": row.stop_reason},
                    reason=reason,
                    result="success",
                    message="Follow-up stopped after issue reassignment",
                    created_by=uid,
                )
            )
    _audit(
        db,
        issue,
        action,
        uid,
        reason,
        old,
        {"owner_user_id": str(target) if target else None},
    )
    add_issue_event(db, issue, action, uid, reason)


@router.post("/issues/{issue_id}/assign")
def assign_issue(
    issue_id: uuid.UUID,
    request: AssignRequest = AssignRequest(),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    issue = _get(db, issue_id)
    _assign(issue, db, _uid(user), request.owner_user_id, None, "assigned")
    db.commit()
    return _serialize(db, issue, True)


@router.post("/issues/{issue_id}/reassign")
def reassign_issue(
    issue_id: uuid.UUID,
    request: ReassignRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    issue = _get(db, issue_id)
    _assign(
        issue,
        db,
        _uid(user),
        request.owner_user_id,
        request.reason.strip(),
        "reassigned",
    )
    db.commit()
    return _serialize(db, issue, True)


@router.post("/issues/{issue_id}/escalate")
def escalate_issue(
    issue_id: uuid.UUID,
    request: EscalateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    issue = _get(db, issue_id)
    uid = _uid(user)
    old = {"status": issue.status}
    issue.status = "escalated"
    _audit(
        db,
        issue,
        "issue_escalated",
        uid,
        request.reason.strip(),
        old,
        {"status": issue.status},
    )
    add_issue_event(db, issue, "escalated", uid, request.reason.strip())
    recipients = [
        x.strip().casefold()
        for x in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
        if x.strip()
    ]
    target = os.getenv("ESCALATION_EMAIL", "").strip().casefold()
    associates = (
        db.query(User)
        .filter(User.role == "associate", User.is_active.is_(True), User.id != uid)
        .all()
    )
    emails = (
        [target]
        if target and target in recipients
        else [u.email for u in associates if u.email.casefold() in recipients]
    )
    invoice = db.get(Invoice, issue.invoice_id)
    for email in emails:
        if invoice and invoice.source_document_id:
            db.add(
                Notification(
                    document_id=invoice.source_document_id,
                    recipient_email=email,
                    subject=f"Invoice issue escalated: {issue.issue_type}",
                    body=issue.description,
                    status="queued",
                    created_by=uid,
                )
            )
    db.commit()
    return _serialize(db, issue, True)


@router.post("/issues/{issue_id}/contact-vendor", status_code=201)
def contact_vendor(
    issue_id: uuid.UUID,
    request: DraftRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    issue = _get(db, issue_id)
    invoice = db.get(Invoice, issue.invoice_id)
    if request.kind not in {
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
    }:
        raise HTTPException(status_code=422, detail="Unsupported email draft kind")
    vendor = (
        db.get(Vendor, invoice.vendor_id) if invoice and invoice.vendor_id else None
    )
    doc = (
        db.get(Document, invoice.source_document_id)
        if invoice and invoice.source_document_id
        else None
    )
    recipient = (
        vendor.contact_email
        if vendor and vendor.contact_email
        else doc.email_sender
        if doc
        else None
    )
    facts = {
        "vendor_name": (
            vendor.name if vendor else invoice.vendor_name_raw if invoice else None
        ),
        "invoice_number": invoice.invoice_no if invoice else None,
        "po_number": invoice.po_number_raw if invoice else None,
        "issue_description": issue.description,
        "expected": issue.expected_value,
        "actual": issue.actual_value,
    }
    draft = create_issue_draft(
        db,
        issue_id=issue.id,
        kind=request.kind,
        facts=facts,
        recipient=recipient,
        user_id=_uid(user),
    )
    add_issue_event(
        db, issue, "vendor_contacted", _uid(user), f"Draft {draft.id} created"
    )
    db.commit()
    db.refresh(draft)
    return {
        "draft_id": draft.id,
        "status": draft.status,
        "blocked_by_allowlist": draft.blocked_by_allowlist,
    }


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: uuid.UUID,
    request: NoteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return _finish(issue_id, "resolved", "resolved", request.note, db, user)


@router.post("/issues/{issue_id}/dismiss")
def dismiss_issue(
    issue_id: uuid.UUID,
    request: NoteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return _finish(issue_id, "dismissed", "dismissed", request.note, db, user)


def _finish(issue_id, status, event, note, db, user):
    issue = _get(db, issue_id)
    uid = _uid(user)
    old = {"status": issue.status}
    issue.status = status
    issue.resolved_at = datetime.now(UTC)
    issue.resolution_note = note.strip()
    _audit(
        db,
        issue,
        f"issue_{event}",
        uid,
        note.strip(),
        old,
        {"status": status, "resolution_note": note.strip()},
    )
    add_issue_event(db, issue, event, uid, note.strip())
    open_count = (
        db.query(InvoiceIssue)
        .filter(
            InvoiceIssue.invoice_id == issue.invoice_id, InvoiceIssue.status.in_(ACTIVE)
        )
        .count()
    )
    invoice = db.get(Invoice, issue.invoice_id)
    if (
        not open_count
        and invoice
        and invoice.workflow_status in {"match_issues", "no_match"}
    ):
        transition_invoice(
            db,
            invoice,
            "match_approved",
            changed_by=uid,
            reason="All invoice issues resolved",
        )
    db.commit()
    return _serialize(db, issue, True)


@router.post("/issues/{issue_id}/reopen")
def reopen_issue(
    issue_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    issue = _get(db, issue_id)
    if issue.status not in {"resolved", "dismissed"}:
        raise HTTPException(
            status_code=409, detail="Only resolved or dismissed issues can be reopened"
        )
    old = {"status": issue.status}
    issue.status = "in_progress"
    issue.resolved_at = None
    issue.resolution_note = None
    _audit(db, issue, "issue_reopened", _uid(user), None, old, {"status": issue.status})
    add_issue_event(db, issue, "reopened", _uid(user), None)
    db.commit()
    return _serialize(db, issue, True)
