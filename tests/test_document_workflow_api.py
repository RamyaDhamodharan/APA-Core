import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import (
    document_routers,
    document_workflow_routers,
    invoice_routers,
)
from src.db.session_db import get_db
from src.schemas.document_workflow_schemas import (
    WorkflowCandidate,
    WorkflowResult,
)

client = TestClient(app)


def test_holding_workflow_is_audited_and_does_not_approve_invoice(monkeypatch):
    document_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    invoice = SimpleNamespace(final_status=None)
    document = SimpleNamespace(id=document_id)
    document.created_by = actor_id
    state = SimpleNamespace(
        decision=None,
        decision_reason=None,
        decision_target_id=None,
        decision_target_type=None,
        decision_by=None,
        decided_at=None,
        updated_at=datetime.now(UTC),
    )
    result = WorkflowResult(
        document_id=document_id,
        status="partial_match",
        available_documents=[],
        missing_document_types=["goods_receipt"],
        candidates=[],
        checks=[],
        recommended_next_actions=["Upload or record the related goods receipt"],
        human_review_required=True,
        human_decision_required=True,
    )
    db = MagicMock()
    db.scalar.side_effect = [document, state]
    monkeypatch.setattr(
        document_workflow_routers,
        "evaluate_document_workflow",
        lambda _db, _document_id, **_kwargs: result,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        response = client.post(
            f"/api/document-workflows/{document_id}/decisions",
            json={"action": "hold", "reason": "Awaiting receipt confirmation"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["workflow"]["status"] == "on_hold"
    assert state.decision == "hold"
    assert invoice.final_status is None
    db.commit.assert_called_once()
    assert db.add.call_args.args[0].action == "document_workflow_hold"


def test_invoice_match_endpoint_returns_partial_workflow_without_approval(monkeypatch):
    invoice_id = uuid.uuid4()
    source_document_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        source_document_id=source_document_id,
        po_id=None,
        final_status="on_hold",
    )
    recommendation = SimpleNamespace(
        matching_status="partial_match",
        status="partial_match",
        supporting_records=[],
        missing_document_types=["goods_receipt"],
        candidates=[],
        checks=[],
        discrepancies=[],
        reason_codes=["missing_goods_receipt"],
        next_recommended_action="Upload or record the related goods receipt",
        human_review_required=True,
        review_status="required",
        processing_status="extracted",
        approval_status="on_hold",
        payment_status="unknown",
        invoice_approval_required=True,
        invoice_id=invoice_id,
        model_dump=lambda mode=None: {
            "invoice_id": str(invoice_id),
            "matching_status": "partial_match",
            "status": "partial_match",
            "supporting_records": [],
            "missing_document_types": ["goods_receipt"],
            "candidates": [],
            "checks": [],
            "discrepancies": [],
            "reason_codes": ["missing_goods_receipt"],
            "next_recommended_action": "Upload or record the related goods receipt",
            "human_review_required": True,
            "review_status": "required",
            "processing_status": "extracted",
            "approval_status": "on_hold",
            "payment_status": "unknown",
            "invoice_approval_required": True,
        },
    )
    db = MagicMock()
    (
        db.query.return_value.outerjoin.return_value.filter.return_value.first.return_value
    ) = invoice
    monkeypatch.setattr(
        invoice_routers,
        "recommend_invoice_review",
        lambda _db, _invoice, owner_id: recommendation,
    )
    monkeypatch.setattr(
        invoice_routers,
        "run_match_for_invoice",
        lambda *_args, **_kwargs: SimpleNamespace(
            id=uuid.uuid4(),
            rules_used={
                "match_outcome": {
                    "outcome": "needs_review",
                    "rule_set_version": "ap-match-v1",
                    "discrepancies": [
                        {
                            "code": "MISSING_GR",
                            "message": "No goods receipt is recorded for this PO.",
                        }
                    ],
                }
            },
        ),
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}
    try:
        response = client.post(f"/api/invoices/{invoice_id}/three-way-match")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["status"] == "review_required"
    assert response.json()["matched"] is False
    assert response.json()["invoice_approval_required"] is True
    assert invoice.final_status == "on_hold"


def test_human_can_confirm_an_extracted_receipt_po_candidate(monkeypatch):
    document_id = uuid.uuid4()
    po_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    vendor = SimpleNamespace(id=uuid.uuid4())
    purchase_order = SimpleNamespace(
        id=po_id,
        is_current=True,
        vendor_id=vendor.id,
    )
    document = SimpleNamespace(
        id=document_id,
        doc_type="goods_receipt",
        extracted_json={"vendor": "Vendor A", "order_number": "PO-1"},
        created_by=actor_id,
    )
    state = SimpleNamespace(
        decision=None,
        decision_reason=None,
        decision_target_id=None,
        decision_target_type=None,
        decision_by=None,
        decided_at=None,
        updated_at=datetime.now(UTC),
    )
    result = WorkflowResult(
        document_id=document_id,
        status="awaiting_human_decision",
        available_documents=[],
        missing_document_types=["po", "invoice"],
        candidates=[
            WorkflowCandidate(
                record_type="purchase_order",
                id=po_id,
                evidence={"po_number": "PO-1"},
            )
        ],
        checks=[],
        recommended_next_actions=["Confirm the correct related record"],
        human_review_required=True,
        human_decision_required=True,
    )
    db = MagicMock()
    db.scalar.side_effect = [document, state, purchase_order, None, None]
    monkeypatch.setattr(
        document_workflow_routers,
        "evaluate_document_workflow",
        lambda _db, _document_id, **_kwargs: result,
    )
    monkeypatch.setattr(
        document_workflow_routers, "resolve_vendor", lambda _db, _name: vendor
    )
    save_receipt = MagicMock()
    monkeypatch.setattr(
        document_workflow_routers,
        "_save_goods_receipt_from_extraction",
        save_receipt,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        response = client.post(
            f"/api/document-workflows/{document_id}/decisions",
            json={
                "action": "confirm_candidate",
                "candidate_type": "purchase_order",
                "candidate_id": str(po_id),
                "reason": "Verified PO number on the receipt",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    save_receipt.assert_called_once_with(
        db,
        document,
        document.extracted_json,
        vendor,
        purchase_order_id=po_id,
    )
    assert response.json()["workflow"]["decision"] == "confirm_candidate"


def test_human_can_select_an_invoice_from_multiple_candidates(monkeypatch):
    document_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    po_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    document = SimpleNamespace(id=document_id, doc_type="po", created_by=actor_id)
    invoice = SimpleNamespace(
        id=invoice_id,
        is_current=True,
        po_id=None,
        po_number_raw="PO-1",
        match_status="waiting_for_po",
        final_status="on_hold",
    )
    purchase_order = SimpleNamespace(id=po_id, is_current=True, po_number="PO-1")
    state = SimpleNamespace(
        decision=None,
        decision_reason=None,
        decision_target_id=None,
        decision_target_type=None,
        decision_by=None,
        decided_at=None,
        updated_at=datetime.now(UTC),
    )
    result = WorkflowResult(
        document_id=document_id,
        status="awaiting_human_decision",
        available_documents=[],
        missing_document_types=["invoice"],
        candidates=[
            WorkflowCandidate(
                record_type="purchase_order",
                id=po_id,
                evidence={"po_number": "PO-1"},
            ),
            WorkflowCandidate(
                record_type="invoice",
                id=invoice_id,
                evidence={"invoice_no": "INV-1", "po_number": "PO-1"},
            ),
        ],
        checks=[],
        recommended_next_actions=["Confirm the correct related record"],
        human_review_required=True,
        human_decision_required=True,
    )
    db = MagicMock()
    db.scalar.side_effect = [document, state, invoice, purchase_order]
    monkeypatch.setattr(
        document_workflow_routers,
        "evaluate_document_workflow",
        lambda _db, _document_id, **_kwargs: result,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        response = client.post(
            f"/api/document-workflows/{document_id}/decisions",
            json={
                "action": "confirm_candidate",
                "candidate_type": "invoice",
                "candidate_id": str(invoice_id),
                "reason": "Assign invoice to its purchase order",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert invoice.po_id == po_id
    assert invoice.final_status == "on_hold"
    assert state.decision_target_type == "invoice"


def test_document_retry_is_audited_and_authenticated(monkeypatch):
    document_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        status="needs_data_review",
        failure_message="Vendor needs review",
        updated_by=None,
        created_by=actor_id,
    )
    db = MagicMock()
    (
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = document
    monkeypatch.setattr(
        document_routers,
        "dispatch_document_processing",
        lambda *_args: None,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        response = client.post(f"/api/documents/{document_id}/retry")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert document.status == "received"
    assert document.updated_by == actor_id
    assert db.add.call_args.args[0].action == "document_processing_retry_requested"
    assert db.add.call_args.args[0].old_value == {"status": "needs_data_review"}


def test_raw_email_retry_uses_attachment_recovery(monkeypatch):
    document_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        status="failed",
        failure_message="Processing stopped unexpectedly after 60 minutes.",
        updated_by=None,
        created_by=actor_id,
        source="email",
        parent_document_id=None,
        original_filename="email.eml",
        mime="message/rfc822",
    )
    db = MagicMock()
    (
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = document
    recovery = MagicMock()
    processing = MagicMock()
    monkeypatch.setattr(document_routers, "dispatch_raw_email_recovery", recovery)
    monkeypatch.setattr(document_routers, "dispatch_document_processing", processing)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        response = client.post(f"/api/documents/{document_id}/retry")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert response.json()["status"] == "parsing"
    recovery.assert_called_once()
    processing.assert_not_called()


def test_stale_document_processing_can_be_retried(monkeypatch):
    document_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        status="parsing",
        failure_message=None,
        updated_by=None,
        created_by=actor_id,
        updated_at=datetime.now(UTC) - timedelta(minutes=15),
    )
    db = MagicMock()
    (
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = document
    monkeypatch.setattr(
        document_routers,
        "dispatch_document_processing",
        lambda *_args: None,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        response = client.post(f"/api/documents/{document_id}/retry")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert document.status == "received"
    assert db.add.call_args.args[0].old_value == {"status": "parsing"}


def test_rejected_candidate_is_audited_and_cannot_be_confirmed_again(monkeypatch):
    document_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    document = SimpleNamespace(id=document_id, created_by=actor_id)
    state = SimpleNamespace(
        decision=None,
        decision_reason=None,
        decision_target_id=None,
        decision_target_type=None,
        decision_by=None,
        decided_at=None,
        updated_at=datetime.now(UTC),
    )
    result = WorkflowResult(
        document_id=document_id,
        status="awaiting_human_decision",
        available_documents=[],
        missing_document_types=["po"],
        candidates=[
            WorkflowCandidate(
                record_type="purchase_order",
                id=candidate_id,
                evidence={"po_number": "PO-1"},
            )
        ],
        checks=[],
        recommended_next_actions=["Confirm the correct related record"],
        human_review_required=True,
        human_decision_required=True,
    )
    db = MagicMock()
    db.scalar.side_effect = [document, state, document, state]
    monkeypatch.setattr(
        document_workflow_routers,
        "evaluate_document_workflow",
        lambda _db, _document_id, **_kwargs: result,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor_id)}
    try:
        rejected = client.post(
            f"/api/document-workflows/{document_id}/decisions",
            json={
                "action": "reject_candidate",
                "candidate_type": "purchase_order",
                "candidate_id": str(candidate_id),
                "reason": "Vendor identity does not match",
            },
        )
        repeated = client.post(
            f"/api/document-workflows/{document_id}/decisions",
            json={
                "action": "confirm_candidate",
                "candidate_type": "purchase_order",
                "candidate_id": str(candidate_id),
                "reason": "Use the verified purchase order",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert rejected.status_code == 200
    assert state.decision == "reject_candidate"
    assert state.decision_target_id == candidate_id
    assert repeated.status_code == 409
    assert db.commit.call_count == 1
