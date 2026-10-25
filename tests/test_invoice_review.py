import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog

client = TestClient(app)


def _setup_review(invoice):
    reviewer_id = uuid.uuid4()
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value.with_for_update.return_value.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(reviewer_id),
        "type": "access",
        "role": "admin",
    }

    def teardown():
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return reviewer_id, db, teardown


def _invoice(match_status="pending"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        po_id=None,
        po_number_raw=None,
        invoice_no="TEST-1",
        match_status=match_status,
        status="needs_review" if match_status == "match_issues" else "ready_to_pay",
        workflow_status="match_approved",
        final_status=None,
        final_by=None,
        final_at=None,
    )


def test_review_invoice_approves_and_audits_decision():
    invoice = _invoice()
    reviewer_id, db, teardown = _setup_review(invoice)
    invoice.final_run_id = uuid.uuid4()
    db.get.return_value = SimpleNamespace(
        rules_used={"match_outcome": {"outcome": "ready_to_pay"}}
    )

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review",
            json={"decision": "approved", "reviewer_id": str(uuid.uuid4())},
        )
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["final_status"] == "approved"
    assert invoice.final_status == "approved"
    assert invoice.final_by == reviewer_id
    assert isinstance(invoice.final_at, datetime)
    db.commit.assert_called_once()

    audit = next(
        item
        for call in db.add.call_args_list
        for item in call.args
        if isinstance(item, AuditLog)
    )
    assert audit.action == "invoice_review_decision"
    assert audit.entity_type == "invoice"
    assert audit.entity_id == invoice.id
    assert audit.created_by == reviewer_id
    assert audit.old_value == {
        "status": "ready_to_pay",
        "final_status": None,
        "workflow_status": "match_approved",
        "final_by": None,
        "final_at": None,
    }
    assert audit.new_value["final_status"] == "approved"
    assert audit.new_value["final_by"] == str(reviewer_id)
    assert audit.message and str(reviewer_id) in audit.message


def test_review_invoice_rejects_invoice():
    invoice = _invoice(match_status="match_issues")
    _, db, teardown = _setup_review(invoice)

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review",
            json={"decision": "rejected", "reason": "Duplicate invoice"},
        )
    finally:
        teardown()

    assert response.status_code == 200
    assert invoice.final_status == "rejected"
    db.commit.assert_called_once()


def test_review_invoice_places_invoice_on_hold():
    invoice = _invoice(match_status="waiting_for_po")
    _, db, teardown = _setup_review(invoice)

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review",
            json={"decision": "on_hold", "reason": "Awaiting clarification"},
        )
    finally:
        teardown()

    assert response.status_code == 200
    assert invoice.final_status == "on_hold"
    db.commit.assert_called_once()


def test_review_invoice_blocks_approval_with_match_issues():
    invoice = _invoice(match_status="match_issues")
    _, db, teardown = _setup_review(invoice)

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review",
            json={"decision": "approved"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    assert invoice.final_status is None
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_review_invoice_blocks_critical_finding_even_if_match_status_is_ready(
    monkeypatch,
):
    import src.api.rest.routers.invoice_routers as invoice_routers

    invoice = _invoice()
    _, db, teardown = _setup_review(invoice)
    monkeypatch.setattr(
        invoice_routers,
        "recommend_invoice_review",
        lambda *_args, **_kwargs: SimpleNamespace(
            checks=[
                SimpleNamespace(outcome="mismatch", severity="error"),
            ]
        ),
    )

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review",
            json={"decision": "approved"},
        )
    finally:
        teardown()

    assert response.status_code == 409
    assert "matching rules pass" in response.json()["detail"]
    assert invoice.final_status is None
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_review_invoice_returns_not_found():
    reviewer_id, db, teardown = _setup_review(None)

    try:
        response = client.post(
            f"/api/invoices/{uuid.uuid4()}/review",
            json={"decision": "rejected"},
        )
    finally:
        teardown()

    assert reviewer_id
    assert response.status_code == 404
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_review_invoice_rejects_invalid_decision():
    invoice = _invoice()
    _, db, teardown = _setup_review(invoice)

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review",
            json={"decision": "maybe"},
        )
    finally:
        teardown()

    assert response.status_code == 422
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_review_invoice_rolls_back_decision_and_audit_on_commit_failure():
    invoice = _invoice()
    _, db, teardown = _setup_review(invoice)
    invoice.final_run_id = uuid.uuid4()
    db.get.return_value = SimpleNamespace(
        rules_used={"match_outcome": {"outcome": "ready_to_pay"}}
    )
    db.commit.side_effect = RuntimeError("audit insert failed")

    try:
        with pytest.raises(RuntimeError, match="audit insert failed"):
            client.post(
                f"/api/invoices/{invoice.id}/review",
                json={"decision": "approved"},
            )
    finally:
        teardown()

    db.rollback.assert_called_once()
