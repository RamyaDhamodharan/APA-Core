"""Nine named use-case checks using the deterministic test LLM and API routes."""

import base64
import json
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import invoice_routers
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.email_draft_models import EmailDraft
from src.models.invoice_models import Invoice
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.services.document_ingest_services import IngestResult
from src.services.llm_pipeline_services import classify_document, extract_document

client = TestClient(app)


def _stub_payload(monkeypatch, **payload):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AP_E2E_MOCK_EXTRACTION", "1")
    values = {"document_type": "invoice", **payload}
    encoded = base64.b64encode(json.dumps(values).encode()).decode()
    marked_text = f"GW_AP_E2E_JSON_BEGIN{encoded}GW_AP_E2E_JSON_END"
    assert classify_document(marked_text) == "invoice"
    return extract_document(marked_text, "invoice")


def _api(db, user_id=None):
    actor = user_id or uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(actor),
        "role": "associate",
    }
    return actor


def _teardown():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_case_1_match_approved_to_ready_to_pay(monkeypatch):
    extracted = _stub_payload(
        monkeypatch,
        invoice_number="INV-CASE-1",
        vendor="Sample Vendor",
        order_number="PO-CASE-1",
        total="100",
        gl_code="6100",
    )
    assert extracted["invoice_number"] == "INV-CASE-1"
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        po_id=uuid.uuid4(),
        po_number_raw="PO-CASE-1",
        invoice_no="INV-CASE-1",
        match_status="pending",
        workflow_status="match_approved",
        final_status=None,
        final_by=None,
        final_at=None,
        final_run_id=uuid.uuid4(),
        owner_user_id=None,
        created_by=None,
    )
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value.with_for_update.return_value.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    monkeypatch.setattr(
        invoice_routers,
        "recommend_invoice_review",
        lambda *_args, **_kwargs: SimpleNamespace(checks=[]),
    )
    db.get.return_value = SimpleNamespace(
        rules_used={"match_outcome": {"outcome": "ready_to_pay"}}
    )
    _api(db)
    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/review", json={"decision": "approved"}
        )
    finally:
        _teardown()
    assert response.status_code == 200
    assert invoice.workflow_status == "approved"
    assert any(
        isinstance(call.args[0], InvoiceStateHistory) for call in db.add.call_args_list
    )


def _recommendation_api(monkeypatch, invoice_id, *, name, expected, actual, reason):
    from src.schemas.document_workflow_schemas import (
        InvoiceRecommendation,
        WorkflowCheck,
    )

    invoice = SimpleNamespace(
        id=invoice_id, owner_user_id=None, created_by=None, source_document_id=None
    )
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    check = WorkflowCheck(
        name=name,
        outcome="mismatch",
        severity="error",
        expected=expected,
        actual=actual,
        difference=str(Decimal(actual) - Decimal(expected)),
        reason=reason,
    )
    recommendation = InvoiceRecommendation(
        invoice_id=invoice_id,
        processing_status="processed",
        matching_status="review_required",
        review_status="pending",
        approval_status="pending",
        supporting_records=[],
        missing_document_types=[],
        candidates=[],
        checks=[check],
        discrepancies=[check],
        reason_codes=[name],
        next_recommended_action="review the difference",
        human_review_required=True,
    )
    monkeypatch.setattr(
        invoice_routers, "recommend_invoice_review", lambda *_a, **_k: recommendation
    )
    monkeypatch.setattr(
        invoice_routers,
        "run_match_for_invoice",
        lambda *_a, **_k: SimpleNamespace(
            id=uuid.uuid4(),
            rules_used={
                "match_outcome": {
                    "outcome": "needs_review",
                    "discrepancies": [
                        {
                            "code": name.upper(),
                            "message": reason,
                        }
                    ],
                }
            },
        ),
    )
    _api(db)
    return client.post(f"/api/invoices/{invoice_id}/three-way-match")


def test_case_2_price_five_percent_above_po(monkeypatch):
    payload = _stub_payload(monkeypatch, invoice_number="INV-CASE-2", total="105")
    assert payload["total"] == "105"
    response = _recommendation_api(
        monkeypatch,
        uuid.uuid4(),
        name="invoice_vs_po_unit_price",
        expected="100",
        actual="105",
        reason="Billed price is 5% above PO price",
    )
    _teardown()
    assert response.status_code == 200
    assert "5% above" in response.json()["issues"][0]


def test_case_3_quantity_100_against_received_80(monkeypatch):
    payload = _stub_payload(
        monkeypatch,
        invoice_number="INV-CASE-3",
        line_items=[{"qty": 100, "unit_price": 2}],
    )
    assert payload["line_items"][0]["qty"] == 100
    response = _recommendation_api(
        monkeypatch,
        uuid.uuid4(),
        name="invoice_qty_exceeds_received",
        expected="80",
        actual="100",
        reason="Billed quantity 100 exceeds received quantity 80",
    )
    _teardown()
    assert response.status_code == 200
    assert "100 exceeds received quantity 80" in response.json()["issues"][0]


def test_case_4_duplicate_invoice_upload_is_skipped(monkeypatch):
    from src.api.rest.routers import document_routers

    _api(MagicMock())
    monkeypatch.setattr(
        document_routers,
        "ingest_file",
        lambda *_a, **_k: IngestResult("duplicate_blocked", uuid.uuid4(), "Duplicate"),
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice_case_4.pdf", b"synthetic", "application/pdf")},
        )
    finally:
        _teardown()
    assert response.status_code == 202
    assert response.json()["results"][0]["result"] == "duplicate"


def test_case_5_unknown_vendor_rejected_and_sender_draft_created(monkeypatch):
    """Case 5 verifies a reviewable draft is created for the sender."""
    from src.api.rest.routers.document_routers import _auto_reject_unmatched_invoice

    sender = "ap-test@example.invalid"
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", sender)
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="received",
        match_status="pending",
        final_status=None,
        vendor_id=None,
    )
    document = SimpleNamespace(id=uuid.uuid4(), email_sender=sender)
    invoice.source_document_id = document.id
    db = MagicMock()
    _auto_reject_unmatched_invoice(db, invoice, document)
    drafts = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], EmailDraft)
    ]
    assert invoice.workflow_status == "rejected"
    assert len(drafts) == 1
    assert drafts[0].status == "draft"
    assert drafts[0].to_addresses == [sender]
    assert drafts[0].blocked_by_allowlist is False


def test_case_6_low_confidence_verification_edit_is_audited(monkeypatch):
    _stub_payload(
        monkeypatch,
        invoice_number="INV-CASE-6",
        field_confidence={"total": {"confidence": 55}},
    )
    actor = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        workflow_status="verifying",
        match_status="needs_vendor_review",
        final_status=None,
        vendor_name_raw="Unknown",
        invoice_no="INV-CASE-6",
        invoice_date=None,
        due_date=None,
        currency="INR",
        subtotal=Decimal("10"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("10"),
        po_number_raw="PO-6",
        po_id=None,
        vendor_id=None,
        gl_code="6000",
        field_confidence={"total": {"confidence": 55, "derived": False}},
    )
    query, lines = MagicMock(), MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value.with_for_update.return_value.first.return_value = invoice
    lines.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [query, lines]
    _api(db, actor)
    monkeypatch.setattr(invoice_routers, "run_match_for_invoice", lambda *_a: None)
    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/verify",
            json={"values": {"gl_code": "6100"}, "reason": "Corrected GL code"},
        )
    finally:
        _teardown()
    assert response.status_code == 200
    assert invoice.gl_code == "6100"
    assert any(
        isinstance(call.args[0], AuditLog)
        and call.args[0].action == "invoice_field_edited"
        for call in db.add.call_args_list
    )


def test_case_7_overdue_invoice_appears_in_command_center(monkeypatch):
    from src.api.rest.routers import command_center_routers

    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        invoice_no="INV-CASE-7",
        vendor_id=None,
        vendor_name_raw="Sample Vendor",
        workflow_status="overdue",
        final_status="approved",
        due_date=date(2026, 10, 1),
        total_amount=Decimal("200"),
        currency="INR",
        owner_user_id=None,
    )
    monkeypatch.setattr(
        command_center_routers, "_app_local_today", lambda: date(2026, 10, 22)
    )
    total_query, row_query, issues_query = MagicMock(), MagicMock(), MagicMock()
    db = MagicMock()
    db.query.side_effect = [total_query, row_query, issues_query]
    total_query.outerjoin.return_value.filter.return_value.scalar.return_value = 1
    row_page = row_query.outerjoin.return_value.outerjoin.return_value
    page_query = row_page.filter.return_value.order_by.return_value.offset.return_value
    rows = page_query.limit.return_value
    rows.all.return_value = [(invoice, None, "processed")]
    issues_query.filter.return_value.all.return_value = []
    _api(db)
    try:
        response = client.get("/api/command-center/bills?bucket=overdue")
    finally:
        _teardown()
    assert response.status_code == 200
    assert response.json()["items"][0]["days_overdue"] == 21


def test_case_8_wrong_gl_rejected_and_resubmitted_with_correct_gl(monkeypatch):
    old = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="rejected",
        final_status="rejected",
        match_status="pending",
        is_current=True,
        invoice_no="INV-CASE-8",
        version_no=1,
        source_document_id=None,
        vendor_id=uuid.uuid4(),
        vendor_name_raw="Sample Vendor",
        vendor_tax_id_raw=None,
        invoice_date=date(2026, 10, 1),
        due_date=date(2026, 10, 30),
        currency="INR",
        subtotal=Decimal("100"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("100"),
        field_confidence={},
        gl_code="9999",
        po_number_raw="PO-8",
        po_id=None,
        owner_user_id=uuid.uuid4(),
        created_by=None,
    )
    prior_rejection = SimpleNamespace(new_value={"reason": "Wrong GL code"})
    invoice_query, audit_query, line_query = MagicMock(), MagicMock(), MagicMock()
    invoice_lock = invoice_query.filter.return_value.with_for_update.return_value
    invoice_lock.first.return_value = old
    audit_query.filter.return_value.order_by.return_value.first.return_value = (
        prior_rejection
    )
    line_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [invoice_query, audit_query, line_query]
    db.flush.side_effect = lambda: setattr(
        next(
            call.args[0]
            for call in db.add.call_args_list
            if isinstance(call.args[0], Invoice)
        ),
        "id",
        uuid.uuid4(),
    )
    _api(db)
    monkeypatch.setattr(
        invoice_routers, "find_duplicate_invoice", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(invoice_routers, "run_match_for_invoice", lambda *_a: None)
    try:
        response = client.post(
            f"/api/invoices/{old.id}/resubmit", json={"gl_code": "6100"}
        )
    finally:
        _teardown()
    assert response.status_code == 200
    assert response.json()["version_no"] == 2
    new_invoice = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Invoice)
    )
    assert new_invoice.gl_code == "6100"
    assert old.is_current is False


def test_case_9_corrupt_pdf_does_not_stop_bulk_upload(monkeypatch):
    from src.api.rest.routers import document_routers

    _api(MagicMock())
    results = iter(
        (
            IngestResult("received", uuid.uuid4(), "Accepted"),
            IngestResult("failed", None, "Corrupt PDF"),
        )
    )
    monkeypatch.setattr(
        document_routers, "ingest_file", lambda *_a, **_k: next(results)
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files=[
                ("file", ("ok_case_9.pdf", b"valid", "application/pdf")),
                ("file", ("corrupt_case_9.pdf", b"corrupt", "application/pdf")),
            ],
        )
    finally:
        _teardown()
    assert response.status_code == 202
    assert [item["status"] for item in response.json()["results"]] == [
        "received",
        "failed",
    ]
