import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.email_draft_models import EmailDraft, EmailDraftEvent
from src.models.invoice_issue_models import InvoiceIssue
from src.models.invoice_models import Invoice
from src.services.email_draft_services import (
    SUPPORTED_KINDS,
    _event,
    _template,
    create_invoice_draft,
    invoice_facts,
    send_draft,
)

router = APIRouter(prefix="/api", tags=["Email drafts"])


class GenerateDraft(BaseModel):
    kind: str


class EditDraft(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1)
    to_addresses: list[str] | None = None
    cc_addresses: list[str] | None = None


class CreateDraftRequest(BaseModel):
    to_addresses: list[str] = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    cc_addresses: list[str] | None = None
    invoice_id: uuid.UUID | None = None
    send_now: bool = False


class SendDraftRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


def _user_id(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _serialize(draft: EmailDraft, events=None):
    result = {
        name: getattr(draft, name)
        for name in (
            "id",
            "kind",
            "invoice_id",
            "issue_id",
            "document_id",
            "to_addresses",
            "cc_addresses",
            "subject",
            "body",
            "status",
            "blocked_by_allowlist",
            "created_by",
            "generated_by",
            "edited_by",
            "sent_by",
            "sent_at",
            "message_id",
            "thread_key",
            "last_error",
            "attempts",
            "created_at",
            "updated_at",
        )
    }
    if events is not None:
        result["events"] = [
            {
                name: getattr(event, name)
                for name in (
                    "id",
                    "event_type",
                    "user_id",
                    "old_value",
                    "new_value",
                    "document_id",
                    "message_id",
                    "created_at",
                )
            }
            for event in events
        ]
    return result


@router.get("/email-drafts")
def list_email_drafts(
    status: str | None = None,
    invoice_id: uuid.UUID | None = Query(default=None, alias="invoice"),
    issue_id: uuid.UUID | None = Query(default=None, alias="issue"),
    kind: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    query = db.query(EmailDraft)
    for column, value in (
        (EmailDraft.status, status),
        (EmailDraft.invoice_id, invoice_id),
        (EmailDraft.issue_id, issue_id),
        (EmailDraft.kind, kind),
    ):
        if value is not None:
            query = query.filter(column == value)
    total = query.count()
    rows = (
        query.order_by(EmailDraft.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_serialize(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/email-drafts/{draft_id}")
def get_email_draft(
    draft_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    draft = db.get(EmailDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Email draft not found")
    events = (
        db.query(EmailDraftEvent)
        .filter_by(draft_id=draft_id)
        .order_by(EmailDraftEvent.created_at)
        .all()
    )
    return _serialize(draft, events)


@router.post("/email-drafts", status_code=201)
def create_custom_email_draft(
    request: CreateDraftRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = _user_id(current_user)
    from src.services.email_draft_services import allowed_recipients

    allowed = allowed_recipients()
    blocked = bool(allowed and "*" not in allowed) and (
        not bool(request.to_addresses)
        or any(
            address.casefold() not in allowed
            for address in [*request.to_addresses, *(request.cc_addresses or [])]
        )
    )

    draft = EmailDraft(
        kind="custom",
        invoice_id=request.invoice_id,
        to_addresses=request.to_addresses,
        cc_addresses=request.cc_addresses or [],
        subject=request.subject,
        body=request.body,
        status="draft",
        blocked_by_allowlist=blocked,
        created_by=user_id,
        generated_by="manual",
    )
    db.add(draft)
    db.flush()
    _event(
        db,
        draft,
        "created",
        user_id,
        old=None,
        new={"to_addresses": draft.to_addresses, "subject": draft.subject, "custom": True},
    )

    sent = False
    message = "Draft saved"
    if request.send_now:
        sent = send_draft(db, draft, user_id)
        if sent:
            db.add(
                AuditLog(
                    action="draft_sent",
                    actor=str(user_id),
                    entity_type="email_draft",
                    entity_id=draft.id,
                    result="success",
                    message="Custom email sent",
                    reason="User composed and sent mail directly from app",
                    created_by=user_id,
                )
            )
            message = "Email sent"
        else:
            message = draft.last_error or "Email could not be sent"

    db.commit()
    db.refresh(draft)
    return {
        "draft": _serialize(draft),
        "sent": sent,
        "message": message,
    }


@router.post("/invoices/{invoice_id}/email-drafts", status_code=201)
def generate_invoice_email_draft(
    invoice_id: uuid.UUID,
    request: GenerateDraft,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if request.kind not in SUPPORTED_KINDS:
        raise HTTPException(status_code=422, detail="Unsupported email draft kind")
    draft = create_invoice_draft(db, invoice, request.kind, _user_id(current_user))
    db.commit()
    db.refresh(draft)
    return _serialize(draft)


@router.post("/issues/{issue_id}/email-drafts", status_code=201)
def generate_issue_email_draft(
    issue_id: uuid.UUID,
    request: GenerateDraft,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    issue = db.get(InvoiceIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    from src.api.rest.routers.issue_routers import DraftRequest, contact_vendor

    return contact_vendor(issue_id, DraftRequest(kind=request.kind), db, current_user)


@router.patch("/email-drafts/{draft_id}")
def edit_email_draft(
    draft_id: uuid.UUID,
    request: EditDraft,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    draft = db.query(EmailDraft).filter_by(id=draft_id).with_for_update().first()
    if draft is None:
        raise HTTPException(status_code=404, detail="Email draft not found")
    if draft.status not in {"draft", "failed"}:
        raise HTTPException(
            status_code=409, detail="Only an unsent email draft can be edited"
        )
    changes = request.model_dump(exclude_unset=True)
    old = {key: getattr(draft, key) for key in changes}
    for key, value in changes.items():
        setattr(draft, key, value)
    from src.services.email_draft_services import is_recipient_allowed

    draft.blocked_by_allowlist = not bool(draft.to_addresses) or any(
        not is_recipient_allowed(address)
        for address in [*draft.to_addresses, *draft.cc_addresses]
    )
    draft.edited_by = _user_id(current_user)
    _event(db, draft, "edited", draft.edited_by, old=old, new=changes)
    db.commit()
    db.refresh(draft)
    return _serialize(draft)


@router.post("/email-drafts/{draft_id}/send")
def send_email_draft(
    draft_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    request: SendDraftRequest | None = None,
):
    draft = db.query(EmailDraft).filter_by(id=draft_id).with_for_update().first()
    if draft is None:
        raise HTTPException(status_code=404, detail="Email draft not found")
    if draft.status == "sent":
        raise HTTPException(
            status_code=409, detail="This email draft has already been sent"
        )
    user_id = _user_id(current_user)
    success = send_draft(db, draft, user_id)
    if success:
        db.add(
            AuditLog(
                action="draft_sent",
                actor=str(user_id),
                entity_type="email_draft",
                entity_id=draft.id,
                result="success",
                message="Email draft sent",
                reason=(request.reason.strip() if request and request.reason else None)
                or "Associate reviewed and sent the draft",
                created_by=user_id,
            )
        )
    if not success and draft.blocked_by_allowlist:
        db.add(
            AuditLog(
                action="email_draft_send_blocked",
                entity_type="email_draft",
                entity_id=draft.id,
                result="warning",
                message="Recipients are not in TEST_MAILBOX_ALLOWLIST",
                created_by=_user_id(current_user),
            )
        )
    db.commit()
    db.refresh(draft)
    return {
        "draft": _serialize(draft),
        "sent": success,
        "message": "Email sent"
        if success
        else draft.last_error or "Email could not be sent",
    }


@router.post("/email-drafts/{draft_id}/discard")
def discard_email_draft(
    draft_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    draft = db.query(EmailDraft).filter_by(id=draft_id).with_for_update().first()
    if draft is None:
        raise HTTPException(status_code=404, detail="Email draft not found")
    if draft.status == "sent":
        raise HTTPException(status_code=409, detail="A sent email cannot be discarded")
    old = {"status": draft.status}
    draft.status = "discarded"
    _event(
        db,
        draft,
        "discarded",
        _user_id(current_user),
        old=old,
        new={"status": "discarded"},
    )
    db.commit()
    return _serialize(draft)


@router.post("/email-drafts/{draft_id}/regenerate")
def regenerate_email_draft(
    draft_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    draft = db.query(EmailDraft).filter_by(id=draft_id).with_for_update().first()
    if draft is None:
        raise HTTPException(status_code=404, detail="Email draft not found")
    if draft.status in {"sent", "discarded"}:
        raise HTTPException(
            status_code=409, detail="This email draft can no longer be regenerated"
        )
    if draft.invoice_id:
        invoice = db.get(Invoice, draft.invoice_id)
        facts = invoice_facts(db, invoice) if invoice else {}
    else:
        facts = {}
    subject, body = _template(draft.kind, facts)
    old = {"subject": draft.subject, "body": draft.body}
    draft.subject, draft.body, draft.generated_by = subject, body, "template"
    draft.edited_by = _user_id(current_user)
    _event(
        db,
        draft,
        "edited",
        draft.edited_by,
        old=old,
        new={"subject": subject, "body": body, "regenerated": True},
    )
    db.commit()
    db.refresh(draft)
    return _serialize(draft)
