import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.rest.routers.invoice_routers import invoice_timeline
from src.models.audit_models import AuditLog
from src.models.email_draft_models import EmailDraftEvent
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder
from src.models.user_models import User


class Query:
    def __init__(self, rows=(), first=None):
        self.rows = list(rows)
        self.first_row = first

    def filter(self, *_args):
        return self

    def filter_by(self, **_kwargs):
        return self

    def join(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.first_row


def _row(**values):
    return SimpleNamespace(**values)


def _timeline_db(invoice, rows):
    calls = []

    def query(model):
        calls.append(model)
        return Query(rows.get(model, ()), first=invoice if model is Invoice else None)

    return SimpleNamespace(query=query), calls


def test_timeline_resolves_actors_once_and_adds_safe_friendly_fields():
    invoice_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    po_id = uuid.uuid4()
    created = datetime.now(UTC)
    invoice = _row(
        id=invoice_id,
        source_document_id=None,
        po_id=po_id,
        po_number_raw="PO-778",
        invoice_no="INV-44",
    )
    edit = _row(
        id=uuid.uuid4(),
        action="invoice_field_edited",
        entity_type="invoice",
        entity_id=invoice_id,
        old_value={"total_amount": "100.00"},
        new_value={"total_amount": "101.00", "reason": "Corrected from source"},
        result="success",
        message="Invoice total verified or edited",
        created_by=actor_id,
        created_at=created,
        step_name=None,
        duration_ms=None,
        input_ref=None,
        output_summary=None,
        reason=None,
        actor=str(actor_id),
    )
    system_step = _row(
        id=uuid.uuid4(),
        action="pipeline_step",
        entity_type="invoice",
        entity_id=invoice_id,
        old_value=None,
        new_value=None,
        result="warning",
        message="Invoice match evaluated",
        created_by=actor_id,
        created_at=created + timedelta(seconds=1),
        step_name="match",
        duration_ms=25,
        input_ref=str(invoice_id),
        output_summary={"result": "match_issues", "differences": 1},
        reason=None,
        actor="system",
    )
    run = _row(
        id=uuid.uuid4(),
        run_no=2,
        po_id=po_id,
        result="match_issues",
        differences=[{"name": "unit_price", "actual": "105", "expected": "100"}],
        created_at=created + timedelta(seconds=2),
        created_by=actor_id,
    )
    db, calls = _timeline_db(
        invoice,
        {
            AuditLog: [edit, system_step],
            InvoiceStateHistory: [],
            EmailDraftEvent: [],
            MatchRun: [run],
            PurchaseOrder: [_row(id=po_id, po_number="PO-778")],
            GoodsReceipt: [_row(po_id=po_id, gr_number="GR-310")],
            User: [_row(id=actor_id, name="Alex Associate", email="alex@example.test")],
        },
    )

    timeline = invoice_timeline(invoice_id, db, {"sub": str(actor_id)})

    assert calls.count(User) == 1
    assert len(timeline) == 3
    assert timeline[0]["kind"] == "audit"
    assert timeline[0]["actor_name"] == "Alex Associate"
    assert timeline[0]["reason"] == "Corrected from source"
    assert timeline[0]["entry_type"] == "user_action"
    assert timeline[0]["old_value"] == {"total_amount": "100.00"}
    assert timeline[0]["new_value"]["total_amount"] == "101.00"
    assert timeline[0]["result"] == "success"
    assert timeline[0]["input_label"] == "Invoice, PO-778, GR-310"
    assert timeline[1]["actor_name"] == "System"
    assert timeline[1]["entry_type"] == "pipeline_step"
    assert timeline[1]["duration_ms"] == 25
    assert timeline[1]["output_label"] == "Match result: Match issues"
    assert timeline[2]["entry_type"] == "pipeline_step"
    assert timeline[2]["run_no"] == 2
    assert timeline[2]["result"] == "warning"
    assert timeline[2]["differences_summary"] == "Price or amount variance"
    assert "actual" not in timeline[2]["output_label"]
    assert [entry["created_at"] for entry in timeline] == sorted(
        entry["created_at"] for entry in timeline
    )


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("invoice_field_edited", "Corrected from source"),
        ("invoice_review_decision", "Rejected: incorrect GL code"),
        ("invoice_review_decision", "Held: receipt is missing"),
    ],
)
def test_timeline_includes_audit_reason_for_edit_reject_and_hold(action, reason):
    invoice_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    created = datetime.now(UTC)
    invoice = _row(
        id=invoice_id,
        source_document_id=None,
        po_id=None,
        po_number_raw=None,
        invoice_no="INV-1",
    )
    audit = _row(
        id=uuid.uuid4(),
        action=action,
        entity_type="invoice",
        entity_id=invoice_id,
        old_value=None,
        new_value={"reason": reason},
        result="success",
        message="Invoice action recorded",
        created_by=actor_id,
        created_at=created,
        step_name=None,
        duration_ms=None,
        input_ref=None,
        output_summary=None,
        reason=None,
        actor=str(actor_id),
    )
    db, _calls = _timeline_db(
        invoice,
        {
            AuditLog: [audit],
            User: [_row(id=actor_id, name="Reviewer", email="reviewer@example.test")],
        },
    )

    result = invoice_timeline(invoice_id, db, {"sub": str(actor_id)})

    assert result[0]["reason"] == reason


def test_timeline_returns_not_found_for_invoice_outside_existing_owner_scope():
    invoice_id = uuid.uuid4()
    db, _calls = _timeline_db(None, {})

    with pytest.raises(HTTPException) as exc:
        invoice_timeline(invoice_id, db, {"sub": str(uuid.uuid4())})

    assert exc.value.status_code == 404
    assert exc.value.detail == "Invoice not found"
