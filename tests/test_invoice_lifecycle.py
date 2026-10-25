import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.api.rest.routers.audit_routers import search_audit_log
from src.api.rest.routers.document_routers import (
    _document_failure_message,
    _record_pipeline_step,
)
from src.api.rest.routers.invoice_routers import (
    record_invoice_payment,
    review_invoice,
    void_invoice_payment,
)
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.schemas.invoice_schemas import InvoiceReviewRequest
from src.services.document_retry_services import (
    is_transient_document_error,
    with_transient_retries,
)
from src.services.invoice_state_services import transition_invoice


def test_transition_writes_history_and_legacy_statuses():
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="match_approved",
        match_status="pending",
        final_status=None,
        final_run_id=uuid.uuid4(),
    )
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        rules_used={"match_outcome": {"outcome": "ready_to_pay"}}
    )
    user_id = uuid.uuid4()

    history = transition_invoice(
        db, invoice, "approved", changed_by=user_id, reason="Verified invoice"
    )

    assert isinstance(history, InvoiceStateHistory)
    assert history.from_status == "match_approved"
    assert history.to_status == "approved"
    assert history.changed_by == user_id
    assert history.created_at.tzinfo == UTC
    assert invoice.final_status == "approved"
    assert invoice.match_status == "pending"
    assert invoice.status == "ready_to_pay"
    db.add.assert_called_once_with(history)


def test_invalid_transition_returns_conflict():
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="paid",
        match_status="pending",
        final_status="approved",
    )
    with pytest.raises(HTTPException) as error:
        transition_invoice(MagicMock(), invoice, "rejected")
    assert error.value.status_code == 409
    assert "paid to rejected" in error.value.detail


def test_associate_can_reach_approval_validation():
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        match_status="pending",
        workflow_status="matching",
        final_status=None,
        final_by=None,
        final_at=None,
    )
    db = MagicMock()
    query = db.query.return_value
    (
        query.outerjoin.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = invoice
    with pytest.raises(HTTPException) as error:
        review_invoice(
            invoice.id,
            InvoiceReviewRequest(decision="approved"),
            db,
            {"sub": str(uuid.uuid4()), "role": "associate"},
        )
    assert error.value.status_code == 409
    assert "matching is unresolved" in error.value.detail


def test_associate_can_hold_with_a_reason():
    reviewer_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        po_id=None,
        po_number_raw=None,
        invoice_no="TEST-1",
        match_status="pending",
        workflow_status="match_approved",
        final_status=None,
        final_by=None,
        final_at=None,
    )
    db = MagicMock()
    query = db.query.return_value
    (
        query.outerjoin.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = invoice
    result = review_invoice(
        invoice.id,
        InvoiceReviewRequest(decision="on_hold", reason="Need vendor clarification"),
        db,
        {"sub": str(reviewer_id), "role": "associate"},
    )
    assert result.final_status == "on_hold"
    assert invoice.workflow_status == "on_hold"
    assert invoice.status == "on_hold"


def test_associate_can_record_payment_once_without_external_payment():
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="approved",
        match_status="pending",
        final_status="approved",
        paid_at=None,
        payment_method=None,
        payment_reference=None,
        updated_by=None,
        total_amount=Decimal("125.50"),
        final_run_id=uuid.uuid4(),
    )
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        rules_used={"match_outcome": {"outcome": "ready_to_pay"}}
    )
    (
        db.query.return_value.filter_by.return_value.with_for_update.return_value.first.return_value
    ) = invoice
    user_id = uuid.uuid4()
    result = record_invoice_payment(
        invoice.id,
        {"paid_date": "2026-10-21", "method": "bank_transfer", "reference": "REF-1"},
        db,
        {"sub": str(user_id), "role": "associate"},
    )
    assert result["workflow_status"] == "paid"
    assert invoice.payment_method == "bank_transfer"
    assert invoice.payment_reference == "REF-1"
    assert invoice.paid_at.tzinfo == UTC
    with pytest.raises(HTTPException) as repeated:
        record_invoice_payment(
            invoice.id,
            {
                "paid_date": "2026-10-21",
                "method": "bank_transfer",
                "reference": "REF-1",
            },
            db,
            {"sub": str(user_id), "role": "associate"},
        )
    assert repeated.value.status_code == 409


def test_void_payment_requires_reason_and_restores_ready_to_pay_state():
    user_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="paid",
        match_status="pending",
        final_status="approved",
        paid_at=datetime.now(UTC),
        payment_method="upi",
        payment_reference="UPI-REF",
        updated_by=None,
        final_run_id=uuid.uuid4(),
    )
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        paid_date=datetime.now(UTC).date(),
        method="upi",
        reference="UPI-REF",
        voided_at=None,
        voided_by=None,
        void_reason=None,
    )
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        rules_used={"match_outcome": {"outcome": "ready_to_pay"}}
    )
    invoice_query, payment_query = MagicMock(), MagicMock()
    db.query.side_effect = [invoice_query, payment_query]
    invoice_lock = invoice_query.filter_by.return_value.with_for_update.return_value
    payment_lock = payment_query.filter.return_value.with_for_update.return_value
    invoice_lock.first.return_value = invoice
    payment_lock.first.return_value = payment

    with pytest.raises(HTTPException) as missing_reason:
        void_invoice_payment(invoice.id, {"reason": "  "}, db, {"sub": str(user_id)})
    assert missing_reason.value.status_code == 422

    response = void_invoice_payment(
        invoice.id,
        {"reason": "Recorded against wrong invoice"},
        db,
        {"sub": str(user_id), "role": "associate"},
    )
    assert response["workflow_status"] == "approved"
    assert invoice.workflow_status == "approved"
    assert payment.voided_by == user_id
    assert payment.void_reason == "Recorded against wrong invoice"
    assert invoice.paid_at is None


def test_payment_unique_index_conflict_returns_clear_conflict():
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        workflow_status="approved",
        final_status="approved",
        total_amount=Decimal("125.50"),
    )
    db = MagicMock()
    locked = db.query.return_value.filter_by.return_value.with_for_update.return_value
    locked.first.return_value = invoice
    db.flush.side_effect = IntegrityError("insert", {}, RuntimeError("unique index"))

    with pytest.raises(HTTPException) as error:
        record_invoice_payment(
            invoice.id,
            {"paid_date": "2026-10-21", "method": "upi", "reference": "REF-2"},
            db,
            {"sub": str(uuid.uuid4()), "role": "associate"},
        )
    assert error.value.status_code == 409
    assert "already recorded" in error.value.detail
    db.rollback.assert_called_once()


def test_document_retry_classifies_transient_and_leaves_scheduling_to_worker():
    attempts = 0
    delays = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("provider temporarily unavailable")
        return "done"

    assert is_transient_document_error(ConnectionError("socket reset"))
    assert not is_transient_document_error(ValueError("corrupt PDF"))
    with pytest.raises(ConnectionError, match="temporarily unavailable"):
        with_transient_retries(operation, sleep=delays.append)
    assert attempts == 1
    assert delays == []


def test_document_retry_does_not_retry_permanent_failures(monkeypatch):
    monkeypatch.setenv("DOCUMENT_PROCESS_MAX_ATTEMPTS", "4")
    attempts = []

    def operation():
        attempts.append(datetime.now(UTC))
        raise ValueError("password protected PDF")

    with pytest.raises(ValueError, match="password protected"):
        with_transient_retries(operation, sleep=lambda _delay: None)
    assert len(attempts) == 1


def test_step_log_records_elapsed_milliseconds_and_compact_output():
    from src.api.rest.routers.document_routers import time

    db = MagicMock()
    document = SimpleNamespace(
        id=uuid.uuid4(), storage_path="incoming/test.pdf", created_by=None
    )
    start = time.perf_counter()
    _record_pipeline_step(
        db, document, "parse", start, output_summary={"characters": 42}
    )
    entry = db.add.call_args.args[0]
    assert entry.step_name == "parse"
    assert entry.duration_ms >= 0
    assert entry.input_ref == "incoming/test.pdf"
    assert entry.output_summary == {"characters": 42}
    assert entry.result == "success"


@pytest.mark.parametrize(
    ("technical", "message"),
    [
        ("password protected", "password-protected"),
        ("corrupt PDF", "damaged"),
        ("unsupported extension", "unsupported"),
        ("image decode failed", "image could not be read"),
        ("provider connection timeout", "temporarily unavailable"),
        ("currency mismatch", "currencies do not match"),
        ("unknown vendor", "vendor could not be identified"),
        ("missing purchase order", "purchase order could not be found"),
        ("missing goods receipt", "goods receipt could not be found"),
    ],
)
def test_document_failure_messages_are_plain_and_actionable(technical, message):
    result = _document_failure_message(
        RuntimeError(technical),
        parse_failed=technical in {"corrupt PDF", "no text could be extracted"},
    )
    assert message in result
    assert technical not in result


def test_empty_parse_is_reported_as_damaged_only_at_parse_stage():
    error = RuntimeError("No text could be extracted from the document")
    assert "damaged" in _document_failure_message(error, parse_failed=True)
    assert "damaged" not in _document_failure_message(error, parse_failed=False)


def test_audit_search_supports_filters_paging_and_hides_technical_detail():
    created_at = datetime.now(UTC)
    user_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        action="pipeline_step",
        entity_type="document",
        entity_id=uuid.uuid4(),
        old_value=None,
        new_value=None,
        result="success",
        message="Document parse step success",
        step_name="parse",
        duration_ms=12,
        input_ref="incoming/a.pdf",
        output_summary={"characters": 12},
        created_by=user_id,
        created_at=created_at,
        technical_detail="must not be returned",
    )
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.offset.return_value = query
    query.limit.return_value.all.return_value = [row]
    query.count.return_value = 1
    db = MagicMock()
    db.query.return_value = query

    response = search_audit_log(
        entity_type="document",
        entity_id=row.entity_id,
        action="pipeline_step",
        result="success",
        user=user_id,
        date_from=created_at,
        date_to=created_at,
        sort="asc",
        page=2,
        page_size=10,
        db=db,
        _current_user={"sub": str(user_id)},
    )
    assert response["total"] == 1
    assert response["page"] == 2
    assert response["items"][0]["step_name"] == "parse"
    assert "technical_detail" not in response["items"][0]
