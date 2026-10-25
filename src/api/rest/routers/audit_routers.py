"""Read-only audit log search API."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from src.api.rest.dependencies import require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice

router = APIRouter(prefix="/api/audit-log", tags=["Audit"])


@router.get("")
def search_audit_log(
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action: str | None = None,
    result: str | None = None,
    user: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    gl_code: str | None = None,
    owner: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    sort_dir: str | None = Query(default=None, pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _current_user: dict = Depends(require_role("admin")),
):
    query = db.query(AuditLog)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(AuditLog.entity_id == entity_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if result:
        query = query.filter(AuditLog.result == result)
    if user:
        query = query.filter(AuditLog.created_by == user)
    if date_from:
        query = query.filter(AuditLog.created_at >= date_from)
    if date_to:
        query = query.filter(AuditLog.created_at <= date_to)
    linked_invoice = exists(
        select(Invoice.id).where(
            or_(
                (AuditLog.entity_type == "invoice")
                & (Invoice.id == AuditLog.entity_id),
                (AuditLog.entity_type == "document")
                & (Invoice.source_document_id == AuditLog.entity_id)
                & exists(
                    select(Document.id).where(
                        Document.id == Invoice.source_document_id
                    )
                ),
            )
        )
    )
    for value, column in (
        (vendor_id, Invoice.vendor_id),
        (invoice_id, Invoice.id),
        (gl_code, Invoice.gl_code),
        (owner, Invoice.owner_user_id),
    ):
        if value is not None:
            query = query.filter(linked_invoice, column == value)
    total = query.count()
    direction = sort_dir or sort
    ordering = (
        AuditLog.created_at.asc()
        if direction == "asc"
        else AuditLog.created_at.desc()
    )
    items = (
        query.order_by(ordering, AuditLog.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    safe_items = [
        {
            "id": item.id,
            "action": item.action,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "old_value": item.old_value,
            "new_value": item.new_value,
            "result": item.result,
            "message": item.message,
            "step_name": item.step_name,
            "duration_ms": item.duration_ms,
            "input_ref": item.input_ref,
            "output_summary": item.output_summary,
            "created_by": item.created_by,
            "created_at": item.created_at,
        }
        for item in items
    ]
    return {"items": safe_items, "total": total, "page": page, "page_size": page_size}
