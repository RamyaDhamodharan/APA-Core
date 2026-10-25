import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.api.rest.routers.invoice_routers import (
    approve_invoice_exception,
    confirm_invoice_match,
    update_invoice,
)
from src.models.audit_models import AuditLog
from src.models.invoice_match_models import InvoiceMatch
from src.models.invoice_models import Invoice
from src.models.purchase_order_models import PurchaseOrder
from src.services.invoice_matching_services import _status_after_match


def test_match_result_statuses_require_clean_po_and_receipt():
    assert (
        _status_after_match(
            po_linked=True,
            result="match_ok",
            has_discrepancies=False,
            missing_receipt=False,
        )
        == "ready_to_pay"
    )
    assert (
        _status_after_match(
            po_linked=False,
            result="no_match",
            has_discrepancies=True,
            missing_receipt=False,
        )
        == "needs_review"
    )
    assert (
        _status_after_match(
            po_linked=True,
            result="match_issues",
            has_discrepancies=True,
            missing_receipt=False,
        )
        == "needs_review"
    )
    assert (
        _status_after_match(
            po_linked=True,
            result="match_ok",
            has_discrepancies=False,
            missing_receipt=True,
        )
        == "needs_review"
    )


def test_confirm_suggested_match_links_po_and_audits(monkeypatch):
    invoice_id, po_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=None,
        status="needs_review",
        match_status="no_match",
        po_id=None,
        po_number_raw=None,
    )
    po = SimpleNamespace(id=po_id, po_number="PO-2026-0001")
    candidate = SimpleNamespace(po_id=po_id, status="suggested", confirmed_by=None)
    invoice_query, match_query, po_query = MagicMock(), MagicMock(), MagicMock()
    invoice_join = invoice_query.outerjoin.return_value
    invoice_filtered = invoice_join.filter.return_value
    invoice_locked = invoice_filtered.with_for_update.return_value
    invoice_locked.first.return_value = invoice
    match_query.filter.return_value.first.return_value = candidate
    po_query.filter.return_value.first.return_value = po
    db = MagicMock()
    db.query.side_effect = lambda model, *args: {
        Invoice: invoice_query,
        InvoiceMatch: match_query,
        PurchaseOrder: po_query,
    }[model]

    def rerun(_db, _invoice_id, _actor_id, **_kwargs):
        invoice.status = "ready_to_pay"
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "src.services.invoice_matching_services.run_match_for_invoice", rerun
    )
    response = confirm_invoice_match(
        invoice_id, {"po_id": str(po_id)}, db, {"sub": str(actor_id)}
    )

    assert response["status"] == "ready_to_pay"
    assert invoice.po_id == po_id
    assert invoice.po_number_raw == "PO-2026-0001"
    assert candidate.status == "confirmed"
    assert candidate.confirmed_by == actor_id
    audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert audit.action == "invoice_match_confirmed"
    assert audit.actor == str(actor_id)
    assert audit.reason
    db.commit.assert_called_once()


def test_approve_exception_cannot_bypass_matching_rules():
    invoice_id, actor_id = uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=None,
        status="needs_review",
        match_status="no_match",
        workflow_status="no_match",
        final_status=None,
        final_by=None,
        final_at=None,
    )
    invoice_query = MagicMock()
    invoice_join = invoice_query.outerjoin.return_value
    invoice_filtered = invoice_join.filter.return_value
    invoice_locked = invoice_filtered.with_for_update.return_value
    invoice_locked.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = invoice_query

    with pytest.raises(HTTPException) as blocked:
        approve_invoice_exception(
            invoice_id,
            {"reason": "Receipt is delayed; supplier confirmed delivery"},
            db,
            {"sub": str(actor_id)},
        )
    assert blocked.value.status_code == 409
    assert invoice.status == "needs_review"
    db.commit.assert_not_called()


def _exception_case(
    *, code="HEADER_TOTAL_VARIANCE", status="needs_review", vendor_id=None
):
    invoice_id, actor_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=None,
        status=status,
        workflow_status="match_issues",
        final_status=None,
        final_by=None,
        final_at=None,
        vendor_id=vendor_id if vendor_id is not None else uuid.uuid4(),
        final_run_id=run_id,
        updated_at=None,
    )
    outcome = {
        "outcome": "needs_review",
        "discrepancies": [{"code": code, "message": "Mismatch"}],
    }
    run = SimpleNamespace(id=run_id, rules_used={"match_outcome": outcome})
    invoice_query = MagicMock()
    (
        invoice_query.outerjoin.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = invoice
    db = MagicMock()
    db.query.return_value = invoice_query
    db.get.return_value = run
    return invoice_id, actor_id, invoice, run, db


def test_approve_exception_allows_total_mismatch_and_audits_reason():
    invoice_id, actor_id, invoice, _run, db = _exception_case()

    result = approve_invoice_exception(
        invoice_id,
        {"reason": "Finance approved the documented total mismatch"},
        db,
        {"sub": str(actor_id)},
    )

    assert result["status"] == "ready_to_pay"
    audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert audit.action == "invoice_exception_approved"
    assert audit.reason == "Finance approved the documented total mismatch"
    assert audit.new_value["reason_codes"] == ["HEADER_TOTAL_VARIANCE"]
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    ("invoice_kwargs", "payload", "detail"),
    [
        (
            {"status": "ready_to_pay"},
            {"reason": "Already reviewed"},
            (
                "This invoice has already been decided. "
                "Refresh to see the current decision."
            ),
        ),
        (
            {"vendor_id": None},
            {"reason": "Vendor review"},
            "Resolve the invoice vendor before approving an exception.",
        ),
    ],
)
def test_approve_exception_reports_current_state_conflicts(
    invoice_kwargs, payload, detail
):
    invoice_id, actor_id, invoice, _run, db = _exception_case(**invoice_kwargs)
    for key, value in invoice_kwargs.items():
        setattr(invoice, key, value)
    with pytest.raises(HTTPException) as blocked:
        approve_invoice_exception(invoice_id, payload, db, {"sub": str(actor_id)})
    assert blocked.value.status_code == 409
    assert blocked.value.detail == detail


def test_approve_exception_reports_stale_review_snapshot():
    invoice_id, actor_id, _invoice, _run, db = _exception_case()
    with pytest.raises(HTTPException) as blocked:
        approve_invoice_exception(
            invoice_id,
            {
                "reason": "Review current evidence",
                "expected_match_run_id": str(uuid.uuid4()),
            },
            db,
            {"sub": str(actor_id)},
        )
    assert blocked.value.status_code == 409
    assert blocked.value.detail == (
        "This invoice changed while you were reviewing it. "
        "Refresh and review the latest data."
    )


def test_approve_exception_reports_disallowed_reason_code():
    invoice_id, actor_id, _invoice, _run, db = _exception_case(code="CURRENCY_MISMATCH")
    with pytest.raises(HTTPException) as blocked:
        approve_invoice_exception(
            invoice_id,
            {"reason": "Business exception requested"},
            db,
            {"sub": str(actor_id)},
        )
    assert blocked.value.status_code == 409
    assert (
        blocked.value.detail
        == "Resolve the currency mismatch before approving an exception."
    )


def test_admin_can_set_active_vendor_through_invoice_patch_and_matching_runs(
    monkeypatch,
):
    invoice_id, vendor_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=actor_id,
        vendor_id=None,
        invoice_no="INV-1",
        invoice_no_normalized="INV1",
        updated_by=None,
        source_document_id=None,
    )
    vendor = SimpleNamespace(id=vendor_id, is_active=True)
    query = MagicMock()
    query.outerjoin.return_value.filter.return_value.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    db.get.return_value = vendor
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers._checked_invoice_number",
        lambda *_args, **_kwargs: "INV1",
    )
    matched = []
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.run_match_for_invoice",
        lambda *_args, **_kwargs: matched.append(True),
    )

    response = update_invoice(
        invoice_id,
        {"vendor_id": str(vendor_id)},
        BackgroundTasks(),
        db,
        {"sub": str(actor_id), "role": "admin"},
    )

    assert response["updated"]["vendor_id"] == str(vendor_id)
    assert invoice.vendor_id == vendor_id
    assert matched == [True]
    db.commit.assert_called_once()


def test_associate_cannot_change_invoice_vendor(monkeypatch):
    invoice_id, actor_id = uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=actor_id,
        vendor_id=None,
        invoice_no="INV-1",
        invoice_no_normalized="INV1",
        updated_by=None,
        source_document_id=None,
    )
    query = MagicMock()
    query.outerjoin.return_value.filter.return_value.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers._checked_invoice_number",
        lambda *_args, **_kwargs: "INV1",
    )

    with pytest.raises(HTTPException) as error:
        update_invoice(
            invoice_id,
            {"vendor_id": str(uuid.uuid4())},
            BackgroundTasks(),
            db,
            {"sub": str(actor_id), "role": "associate"},
        )

    assert getattr(error.value, "status_code", None) == 403
    db.commit.assert_not_called()
