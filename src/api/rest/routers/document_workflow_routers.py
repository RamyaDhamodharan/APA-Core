import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.api.rest.routers.document_routers import (
    _document_owner_filters,
    _save_goods_receipt_from_extraction,
)
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.document_workflow_models import DocumentWorkflow
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.schemas.document_workflow_schemas import (
    WorkflowDecisionRequest,
    WorkflowDecisionResponse,
    WorkflowResult,
)
from src.services.document_workflow_services import evaluate_document_workflow
from src.services.invoice_state_services import (
    legacy_workflow_status,
    transition_invoice,
)
from src.services.po_matching_services import normalize_value
from src.services.vendor_resolution_services import resolve_vendor

router = APIRouter(
    prefix="/api/document-workflows",
    tags=["Document Workflows"],
    dependencies=[Depends(get_current_user)],
)


def _actor_id(user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _state(db: Session, document_id: uuid.UUID) -> DocumentWorkflow:
    state = db.scalar(
        select(DocumentWorkflow).where(DocumentWorkflow.document_id == document_id)
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Document workflow not found")
    return state


def _load_result(
    db: Session, document_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[DocumentWorkflow, WorkflowResult]:
    try:
        result = evaluate_document_workflow(db, document_id, owner_id=owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    state = db.scalar(
        select(DocumentWorkflow).where(DocumentWorkflow.document_id == document_id)
    )
    if state is None:
        state = DocumentWorkflow(
            document_id=document_id, status=result.status, result={}
        )
        db.add(state)
        db.flush()
    if state.decision == "hold":
        result = result.model_copy(
            update={"status": "on_hold", "human_review_required": True}
        )
    elif state.decision == "reject_candidate":
        result = result.model_copy(
            update={
                "status": "awaiting_human_decision",
                "human_decision_required": True,
            }
        )
    result = result.model_copy(
        update={
            "decision": state.decision,
            "decision_reason": state.decision_reason,
            "decision_target_id": state.decision_target_id,
            "decision_target_type": state.decision_target_type,
            "updated_at": state.updated_at,
        }
    )
    state.status = result.status
    state.result = result.model_dump(mode="json")
    return state, result


@router.get("/{document_id}", response_model=WorkflowResult)
def get_document_workflow(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkflowResult:
    owner_id = _actor_id(current_user)
    document = db.query(Document).filter(
        Document.id == document_id, *_document_owner_filters(owner_id)
    ).first()
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    _, result = _load_result(db, document_id, owner_id)
    db.commit()
    return result


@router.post("/{document_id}/decisions", response_model=WorkflowDecisionResponse)
def decide_document_workflow(
    document_id: uuid.UUID,
    payload: WorkflowDecisionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkflowDecisionResponse:
    actor_id = _actor_id(current_user)
    document = db.scalar(
        select(Document)
        .where(Document.id == document_id, *_document_owner_filters(actor_id))
        .with_for_update()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    state, result = _load_result(db, document_id, actor_id)
    old_decision = state.decision
    target = None
    if payload.action in {"hold", "confirm_candidate", "reject_candidate"} and not (
        payload.reason or ""
    ).strip():
        raise HTTPException(status_code=422, detail="A reason is required")
    rejected_candidate_can_be_replaced = (
        state.decision == "reject_candidate"
        and payload.action == "confirm_candidate"
        and payload.candidate_id != state.decision_target_id
    )
    if (
        state.decision == "hold"
        and payload.action != "retry_match"
    ) or (
        state.decision == "reject_candidate"
        and payload.action != "retry_match"
        and not rejected_candidate_can_be_replaced
    ):
        raise HTTPException(
            status_code=409,
            detail="Retry matching before making another workflow decision",
        )

    if payload.action == "confirm_candidate":
        if payload.candidate_id is None or payload.candidate_type is None:
            raise HTTPException(
                status_code=422, detail="A candidate ID and type are required"
            )
        if not any(
            candidate.id == payload.candidate_id
            and candidate.record_type == payload.candidate_type
            for candidate in result.candidates
        ):
            raise HTTPException(status_code=409, detail="Candidate is no longer valid")
        if payload.candidate_type == "purchase_order":
            target = db.scalar(
                select(PurchaseOrder)
                .where(PurchaseOrder.id == payload.candidate_id)
                .with_for_update()
            )
            if target is None or not target.is_current:
                raise HTTPException(
                    status_code=409, detail="Purchase order is no longer current"
                )
            invoice = db.scalar(
                select(Invoice)
                .where(Invoice.source_document_id == document_id)
                .with_for_update()
            )
            receipt = db.scalar(
                select(GoodsReceipt)
                .where(GoodsReceipt.source_document_id == document_id)
                .with_for_update()
            )
            if invoice is not None:
                invoice.po_id = target.id
                if legacy_workflow_status(invoice) not in {
                    "approved", "rejected", "on_hold", "paid"
                }:
                    transition_invoice(
                        db,
                        invoice,
                        "matching",
                        changed_by=actor_id,
                        reason=payload.reason or "Purchase order candidate confirmed",
                    )
            elif receipt is not None:
                if receipt.vendor_id != target.vendor_id:
                    raise HTTPException(
                        status_code=422,
                        detail="Receipt vendor does not match selected PO",
                    )
                po_line_ids = set(
                    db.scalars(
                        select(PurchaseOrderLine.id).where(
                            PurchaseOrderLine.po_id == target.id
                        )
                    ).all()
                )
                if any(line.po_line_id not in po_line_ids for line in receipt.lines):
                    raise HTTPException(
                        status_code=422,
                        detail="Receipt lines do not belong to selected PO",
                    )
                receipt.po_id = target.id
            elif document.doc_type == "goods_receipt" and isinstance(
                document.extracted_json, dict
            ):
                vendor_name = str(document.extracted_json.get("vendor") or "").strip()
                vendor = resolve_vendor(db, vendor_name)
                if vendor is None:
                    raise HTTPException(
                        status_code=422,
                        detail="Receipt vendor could not be resolved",
                    )
                try:
                    _save_goods_receipt_from_extraction(
                        db,
                        document,
                        document.extracted_json,
                        vendor,
                        purchase_order_id=target.id,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            else:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This document has no persisted invoice or receipt to associate"
                    ),
                )
        elif payload.candidate_type == "invoice":
            target = db.scalar(
                select(Invoice)
                .where(Invoice.id == payload.candidate_id)
                .with_for_update()
            )
            if target is None or not target.is_current:
                raise HTTPException(
                    status_code=409, detail="Invoice is no longer current"
                )
            po_candidates = [
                candidate
                for candidate in result.candidates
                if candidate.record_type == "purchase_order"
            ]
            if target.po_id:
                selected_po = db.scalar(
                    select(PurchaseOrder)
                    .where(PurchaseOrder.id == target.po_id)
                    .with_for_update()
                )
            elif len(po_candidates) == 1:
                selected_po = db.scalar(
                    select(PurchaseOrder)
                    .where(PurchaseOrder.id == po_candidates[0].id)
                    .with_for_update()
                )
            else:
                selected_po = None
            if selected_po is None or not selected_po.is_current:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Confirm a unique current purchase order before selecting "
                        "the invoice"
                    ),
                )
            if target.po_number_raw and normalize_value(
                target.po_number_raw
            ) != normalize_value(selected_po.po_number):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Invoice PO reference does not match selected purchase "
                        "order"
                    ),
                )
            target.po_id = selected_po.id
            if legacy_workflow_status(target) not in {
                "approved", "rejected", "on_hold", "paid"
            }:
                transition_invoice(
                    db,
                    target,
                    "matching",
                    changed_by=actor_id,
                    reason=payload.reason or "Purchase order candidate confirmed",
                )
        else:
            raise HTTPException(
                status_code=422,
                detail="This candidate type cannot be associated through this action",
            )
    elif payload.action == "reject_candidate":
        if payload.candidate_id is None or payload.candidate_type is None:
            raise HTTPException(
                status_code=422, detail="A candidate ID and type are required"
            )
        if not any(
            candidate.id == payload.candidate_id
            and candidate.record_type == payload.candidate_type
            for candidate in result.candidates
        ):
            raise HTTPException(status_code=409, detail="Candidate is no longer valid")
    elif (
        payload.action == "request_partial_review" and result.status != "partial_match"
    ):
        raise HTTPException(
            status_code=409, detail="Only a partial comparison can be sent for review"
        )

    if payload.action == "retry_match":
        state.decision = None
        state.decision_target_id = None
        state.decision_target_type = None
        state.decision_reason = None
        state.decision_by = None
        state.decided_at = None
        _, result = _load_result(db, document_id, actor_id)
        action_name = "document_workflow_retried"
    else:
        state.decision = payload.action
        state.decision_target_id = payload.candidate_id
        state.decision_target_type = payload.candidate_type
        state.decision_reason = payload.reason
        state.decision_by = actor_id
        state.decided_at = datetime.now(UTC)
        action_name = f"document_workflow_{payload.action}"
        if payload.action == "hold":
            result = result.model_copy(
                update={"status": "on_hold", "human_review_required": True}
            )
        elif payload.action in {"reject_candidate", "request_partial_review"}:
            result = result.model_copy(
                update={
                    "status": "awaiting_human_decision",
                    "human_decision_required": True,
                }
            )
        elif payload.action == "confirm_candidate":
            result = evaluate_document_workflow(db, document_id, owner_id=actor_id)

    result = result.model_copy(
        update={
            "decision": state.decision,
            "decision_reason": state.decision_reason,
            "decision_target_id": state.decision_target_id,
            "decision_target_type": state.decision_target_type,
        }
    )
    state.status = result.status
    state.result = result.model_dump(mode="json")
    db.add(
        AuditLog(
            action=action_name,
            entity_type="document_workflow",
            entity_id=document_id,
            old_value={"decision": old_decision},
            new_value={
                "decision": payload.action,
                "target_id": str(payload.candidate_id)
                if payload.candidate_id
                else None,
                "target_type": payload.candidate_type,
                "reason": payload.reason,
            },
            message=f"Document workflow action: {payload.action}",
            created_by=actor_id,
        )
    )
    try:
        db.commit()
        db.refresh(state)
    except Exception:
        db.rollback()
        raise
    result = result.model_copy(update={"updated_at": state.updated_at})
    return WorkflowDecisionResponse(
        workflow=result,
        decision=payload.action,
        decided_by=actor_id,
        decided_at=state.decided_at or state.updated_at,
    )
