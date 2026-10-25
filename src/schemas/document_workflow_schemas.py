import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

WorkflowStatus = Literal[
    "processing",
    "extraction_failed",
    "awaiting_related_documents",
    "partial_match",
    "matched",
    "review_required",
    "awaiting_human_decision",
    "on_hold",
]


class WorkflowDocument(BaseModel):
    id: uuid.UUID
    type: str
    reference: str | None = None


class WorkflowCandidate(BaseModel):
    record_type: Literal["purchase_order", "goods_receipt", "invoice"]
    id: uuid.UUID
    evidence: dict[str, str | int | Decimal | None]


class WorkflowCheck(BaseModel):
    finding_id: str | None = None
    category: str | None = None
    finding_status: Literal["supported", "open", "not_verifiable"] | None = None
    evidence_record_ids: list[uuid.UUID] = Field(default_factory=list)
    name: str
    reason_code: str | None = None
    outcome: Literal["passed", "mismatch", "not_possible"]
    severity: Literal["info", "warning", "error"] = "info"
    expected: str | Decimal | None = None
    actual: str | Decimal | None = None
    difference: str | Decimal | None = None
    reason: str


class WorkflowResult(BaseModel):
    document_id: uuid.UUID
    status: WorkflowStatus
    available_documents: list[WorkflowDocument]
    missing_document_types: list[str]
    candidates: list[WorkflowCandidate]
    checks: list[WorkflowCheck]
    recommended_next_actions: list[str]
    human_review_required: bool
    human_decision_required: bool
    invoice_approval_required: bool = True
    decision: str | None = None
    decision_reason: str | None = None
    decision_target_id: uuid.UUID | None = None
    decision_target_type: Literal[
        "purchase_order", "goods_receipt", "invoice"
    ] | None = None
    updated_at: datetime | None = None


class InvoiceRecommendation(BaseModel):
    invoice_id: uuid.UUID
    processing_status: str
    matching_status: str
    review_status: str
    approval_status: str
    payment_status: Literal["unknown"] = "unknown"
    supporting_records: list[WorkflowDocument]
    missing_document_types: list[str]
    candidates: list[WorkflowCandidate]
    checks: list[WorkflowCheck]
    discrepancies: list[WorkflowCheck]
    reason_codes: list[str]
    next_recommended_action: str
    human_review_required: bool
    invoice_approval_required: bool = True


class WorkflowDecisionRequest(BaseModel):
    action: Literal[
        "confirm_candidate",
        "reject_candidate",
        "hold",
        "request_partial_review",
        "retry_match",
    ]
    candidate_id: uuid.UUID | None = None
    candidate_type: Literal["purchase_order", "goods_receipt", "invoice"] | None = None
    reason: str | None = Field(default=None, max_length=500)


class WorkflowDecisionResponse(BaseModel):
    workflow: WorkflowResult
    decision: str
    decided_by: uuid.UUID
    decided_at: datetime
