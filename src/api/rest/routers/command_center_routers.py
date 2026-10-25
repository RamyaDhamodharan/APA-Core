"""Command center invoice buckets, summaries, and assignment choices."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Float, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.api.rest.routers.invoice_routers import _app_local_today
from src.db.session_db import get_db
from src.models.document_models import Document
from src.models.invoice_issue_models import InvoiceIssue
from src.models.invoice_models import Invoice
from src.models.match_run_models import MatchRun
from src.models.vendor_models import Vendor

router = APIRouter(prefix="/api/command-center", tags=["Command center"])
ACTIVE_ISSUES = {"open", "in_progress", "waiting_vendor", "escalated"}


def _bucket_conditions(today: date):
    issue_exists = exists(
        select(InvoiceIssue.id).where(
            InvoiceIssue.invoice_id == Invoice.id,
            InvoiceIssue.status.in_(ACTIVE_ISSUES),
        )
    )
    saved_ready_match = exists(
        select(MatchRun.id).where(
            MatchRun.id == Invoice.final_run_id,
            MatchRun.rules_used["match_outcome"]["outcome"].as_string()
            == "ready_to_pay",
        )
    )
    return {
        "ready_to_pay": (
            Invoice.status == "ready_to_pay",
            saved_ready_match,
            Invoice.paid_at.is_(None),
            or_(Invoice.due_date.is_(None), Invoice.due_date >= today),
        ),
        "needs_review": (
            or_(
                Invoice.status == "needs_review",
                and_(Invoice.status == "ready_to_pay", ~saved_ready_match),
            ),
            Invoice.paid_at.is_(None),
            Invoice.final_status != "rejected",
        ),
        "overdue": (
            Invoice.due_date < today,
            Invoice.paid_at.is_(None),
            Invoice.workflow_status != "paid",
            Invoice.final_status != "rejected",
            Invoice.workflow_status != "rejected",
        ),
        "rejected": or_(
            Invoice.workflow_status == "rejected", Invoice.final_status == "rejected"
        ),
        "needs_attention": (
            or_(
                Invoice.status == "needs_review",
                Invoice.workflow_status == "verifying",
                issue_exists,
                Invoice.owner_user_id.is_(None),
            ),
            Invoice.workflow_status.not_in({"paid", "rejected"}),
            Invoice.final_status != "rejected",
        ),
    }


def _bucket_filter(name, today):
    value = _bucket_conditions(today)[name]
    if isinstance(value, tuple):
        from sqlalchemy import and_

        return and_(*value)
    return value


@router.get("/summary")
def command_center_summary(
    db: Session = Depends(get_db), _user: dict = Depends(get_current_user)
):
    today = _app_local_today()
    result = {}
    for bucket in (
        "ready_to_pay",
        "overdue",
        "rejected",
        "needs_review",
        "needs_attention",
        "all",
    ):
        condition = (
            Invoice.is_current.is_(True)
            if bucket == "all"
            else _bucket_filter(bucket, today)
        )
        count, amount = (
            db.query(
                func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0)
            )
            .filter(Invoice.is_current.is_(True), condition)
            .one()
        )
        result[bucket] = {"count": count, "total_amount": amount}
    return {"buckets": result, "as_of": today}


@router.get("/bills")
def command_center_bills(
    bucket: Annotated[
        str,
        Query(
            pattern="^(ready_to_pay|needs_review|overdue|rejected|needs_attention|all)$"
        ),
    ] = "all",
    vendor_id: uuid.UUID | None = None,
    workflow_status: str | None = None,
    doc_bucket: str | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    gl_code: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    confidence_min: float | None = Query(default=None, ge=0, le=100),
    sort_by: Annotated[
        str, Query(pattern="^(due_date|amount|days_overdue|confidence|date_received)$")
    ] = "date_received",
    sort_dir: Literal["asc", "desc"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    if due_date_from and due_date_to and due_date_from > due_date_to:
        raise HTTPException(
            status_code=422, detail="due_date_from must not exceed due_date_to"
        )
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(
            status_code=422, detail="amount_min must not exceed amount_max"
        )
    today = _app_local_today()
    filters = [Invoice.is_current.is_(True)]
    if bucket != "all":
        filters.append(_bucket_filter(bucket, today))
    if vendor_id:
        filters.append(Invoice.vendor_id == vendor_id)
    if workflow_status:
        filters.append(Invoice.workflow_status == workflow_status)
    if doc_bucket:
        filters.append(Document.doc_type == doc_bucket)
    if due_date_from:
        filters.append(Invoice.due_date >= due_date_from)
    if due_date_to:
        filters.append(Invoice.due_date <= due_date_to)
    if amount_min is not None:
        filters.append(Invoice.total_amount >= amount_min)
    if amount_max is not None:
        filters.append(Invoice.total_amount <= amount_max)
    if gl_code:
        filters.append(Invoice.gl_code == gl_code)
    if owner_user_id:
        filters.append(Invoice.owner_user_id == owner_user_id)
    if confidence_min is not None:
        filters.append(
            cast(Invoice.field_confidence["total"]["confidence"].as_float(), Float)
            >= confidence_min
        )
    total = (
        db.query(func.count(Invoice.id))
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(*filters)
        .scalar()
    )
    sort_column = {
        "due_date": Invoice.due_date,
        "days_overdue": Invoice.due_date,
        "amount": Invoice.total_amount,
        "confidence": cast(
            Invoice.field_confidence["total"]["confidence"].as_float(), Float
        ),
        "date_received": Invoice.created_at,
    }[sort_by]
    descending = (
        (sort_dir == "desc") if sort_dir else sort_by in {"amount", "date_received"}
    )
    ordering = sort_column.desc() if descending else sort_column.asc()
    if sort_by != "date_received":
        ordering = ordering.nullslast()
    rows = (
        db.query(Invoice, Vendor.name.label("vendor_name"), Document.status)
        .outerjoin(Vendor, Invoice.vendor_id == Vendor.id)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(*filters)
        .order_by(ordering, Invoice.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    issue_rows = (
        db.query(InvoiceIssue)
        .filter(
            InvoiceIssue.invoice_id.in_([row[0].id for row in rows]),
            InvoiceIssue.status.in_(ACTIVE_ISSUES),
        )
        .all()
        if rows
        else []
    )
    issue_by_invoice = {}
    for issue in issue_rows:
        issue_by_invoice.setdefault(issue.invoice_id, []).append(issue)
    items = []
    for invoice, vendor_name, document_status in rows:
        issues = issue_by_invoice.get(invoice.id, [])
        if invoice.workflow_status == "match_approved":
            reason = "waiting for approval"
        elif any(issue.status == "waiting_vendor" for issue in issues):
            reason = "waiting vendor"
        elif issues:
            reason = "open issues"
        elif invoice.workflow_status == "on_hold":
            reason = "on hold"
        elif invoice.owner_user_id is None:
            reason = "unassigned"
        elif invoice.workflow_status == "overdue":
            reason = "overdue awaiting payment"
        else:
            reason = None
        status = invoice.workflow_status
        actions = ["view", "escalate", "reassign"]
        if status in {"match_approved", "match_issues", "no_match", "verifying"}:
            actions.extend(["approve", "reject"])
        if status in {"approved", "overdue"}:
            actions.append("pay")
        if status not in {"paid", "rejected", "on_hold"}:
            actions.append("hold")
        if status == "on_hold":
            actions.append("release_hold")
        if status == "rejected":
            actions.append("resubmit")
        items.append(
            {
                "invoice_id": invoice.id,
                "source_document_id": invoice.source_document_id,
                "invoice_no": invoice.invoice_no,
                "vendor_id": invoice.vendor_id,
                "vendor_name": vendor_name or invoice.vendor_name_raw,
                "workflow_status": status,
                "status": getattr(invoice, "status", status),
                "final_status": invoice.final_status,
                "due_date": invoice.due_date,
                "total_amount": invoice.total_amount,
                "currency": invoice.currency,
                "owner_user_id": invoice.owner_user_id,
                "days_overdue": max(0, (today - invoice.due_date).days)
                if invoice.due_date and invoice.due_date < today
                else 0,
                "reason": reason,
                "alert": bool(
                    invoice.due_date
                    and invoice.due_date < today
                    and status not in {"paid", "rejected"}
                ),
                "can_resubmit": status == "rejected",
                "rejection_reason": _rejection_reason(db, invoice.id)
                if status == "rejected"
                else None,
                "actions": actions,
                "processing_status": document_status,
            }
        )
    return {"items": items, "total": total or 0, "page": page, "page_size": page_size}


def _rejection_reason(db, invoice_id):
    from src.models.audit_models import AuditLog

    audit = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "invoice",
            AuditLog.entity_id == invoice_id,
            AuditLog.action == "invoice_review_decision",
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    return (audit.new_value or {}).get("reason") if audit and audit.new_value else None
