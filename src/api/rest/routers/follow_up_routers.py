import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.follow_up_models import FollowUp

router = APIRouter(prefix="/api/follow-ups", tags=["Follow-ups"])


class StopFollowUpRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def require_nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()


@router.get("")
def list_follow_ups(
    entity_type: str | None = Query(default=None, pattern="^(invoice|issue)$"),
    entity_id: uuid.UUID | None = None,
    kind: str | None = None,
    stopped: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    query = db.query(FollowUp)
    if entity_type:
        query = query.filter(FollowUp.entity_type == entity_type)
    if entity_id:
        query = query.filter(FollowUp.entity_id == entity_id)
    if kind:
        query = query.filter(FollowUp.kind == kind)
    if stopped is True:
        query = query.filter(FollowUp.stopped_at.is_not(None))
    elif stopped is False:
        query = query.filter(FollowUp.stopped_at.is_(None))
    total = query.count()
    rows = (
        query.order_by(FollowUp.next_due_at, FollowUp.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    fields = (
        "id", "entity_type", "entity_id", "kind", "recipient", "attempts",
        "last_sent_at", "next_due_at", "stopped_at", "stop_reason", "created_at",
    )
    return {
        "items": [{field: getattr(row, field) for field in fields} for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{follow_up_id}/stop")
def stop_follow_up(
    follow_up_id: uuid.UUID,
    request: StopFollowUpRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    row = (
        db.query(FollowUp)
        .filter_by(id=follow_up_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    if row.stopped_at is not None:
        raise HTTPException(status_code=409, detail="Follow-up is already stopped")
    user_id = uuid.UUID(str(current_user["sub"]))
    now = datetime.now(UTC)
    row.stopped_at = now
    row.stop_reason = request.reason
    db.add(AuditLog(
        action="follow_up_stopped", entity_type="follow_up", entity_id=row.id,
        old_value={"stopped_at": None},
        new_value={"stopped_at": now.isoformat(), "stop_reason": request.reason},
        reason=request.reason, result="success", message="Follow-up stopped by user",
        created_by=user_id,
    ))
    db.commit()
    return {"id": row.id, "stopped_at": row.stopped_at, "stop_reason": row.stop_reason}
