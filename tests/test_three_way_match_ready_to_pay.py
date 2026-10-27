import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services import ap_decision_engine as engine
from src.services import invoice_matching_services as matching


def _make_datasets(invoice_id, po_id, vendor_id, po_line_id, accepted_qty="10"):
    po = SimpleNamespace(
        id=po_id,
        po_number="PO-100",
        vendor_id=vendor_id,
        currency="USD",
        tax_amount=Decimal("0"),
        total_amount=Decimal("100"),
        is_current=True,
    )
    po_line = SimpleNamespace(
        id=po_line_id,
        po_id=po_id,
        item_code="SKU-100",
        description="Widget A",
        qty=Decimal("10"),
        unit_price=Decimal("10"),
        line_amount=Decimal("100"),
    )
    po.lines = [po_line]

    gr = SimpleNamespace(id=uuid.uuid4(), po_id=po_id, vendor_id=vendor_id)
    gr_line = SimpleNamespace(
        po_line_id=po_line_id,
        goods_receipt_id=gr.id,
        accepted_qty=Decimal(accepted_qty),
        received_qty=Decimal(accepted_qty),
        rejected_qty=Decimal("0"),
    )
    gr.lines = [gr_line]

    inv_line = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=invoice_id,
        item_code="SKU-100",
        description="Widget A",
        qty=Decimal("10"),
        unit_price=Decimal("10"),
        line_amount=Decimal("100"),
        po_line_id=po_line_id,
        field_confidence={
            key: {"confidence": 90} for key in ("qty", "unit_price", "line_amount")
        },
    )
    invoice = SimpleNamespace(
        id=invoice_id,
        po_id=po_id,
        po_number_raw="PO-100",
        vendor_id=vendor_id,
        vendor_name_raw="Test Vendor",
        invoice_no="INV-100",
        invoice_no_normalized="INV100",
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=Decimal("100"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("100"),
        field_confidence={key: {"confidence": 90} for key in engine.MATERIAL_FIELDS},
        status="processing",
        workflow_status="matching",
        match_status="pending",
        final_status=None,
        final_run_id=None,
        final_by=None,
        source_document_id=None,
    )

    datasets = {
        PurchaseOrder: [po],
        PurchaseOrderLine: [po_line],
        GoodsReceipt: [gr],
        GoodsReceiptLine: [gr_line],
        InvoiceLine: [inv_line],
    }
    return invoice, po, gr, gr_line, datasets


def test_clean_three_way_match_makes_invoice_ready_to_pay(monkeypatch):
    """When an invoice, PO, and goods receipt match correctly, the invoice must become ready_to_pay."""
    invoice_id, po_id, vendor_id, po_line_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice, po, gr, gr_line, datasets = _make_datasets(invoice_id, po_id, vendor_id, po_line_id, "10")

    db = MagicMock()
    db.get.side_effect = lambda model, ident: (
        invoice if model == Invoice and ident == invoice_id
        else po if model == PurchaseOrder and ident == po_id
        else None
    )

    def mock_query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.options.return_value = q
        q.order_by.return_value = q
        res = datasets.get(model, [])
        q.all.return_value = res
        q.first.return_value = res[0] if res else None
        q.scalar.return_value = 0
        return q

    db.query.side_effect = mock_query

    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("src.services.ap_decision_engine.find_duplicate_invoice", lambda *a, **k: None)
    monkeypatch.setattr("src.services.llm_matching_services.llm_evaluate_invoice_match", lambda *a, **k: None)

    run = matching.run_match_for_invoice(db, invoice_id, force=True)

    assert run is not None
    assert run.result == "match_ok"
    assert invoice.status == "ready_to_pay"
    assert invoice.workflow_status == "match_approved"
    assert run.rules_used["match_outcome"]["outcome"] == "ready_to_pay"


def test_mismatched_quantity_requires_review_until_goods_receipt_matches(monkeypatch):
    """If received quantity is less than invoice quantity, invoice needs review until receipt matches."""
    invoice_id, po_id, vendor_id, po_line_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice, po, gr, gr_line, datasets = _make_datasets(invoice_id, po_id, vendor_id, po_line_id, "5")

    db = MagicMock()
    db.get.side_effect = lambda model, ident: (
        invoice if model == Invoice and ident == invoice_id
        else po if model == PurchaseOrder and ident == po_id
        else None
    )

    def mock_query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.options.return_value = q
        q.order_by.return_value = q
        res = datasets.get(model, [])
        q.all.return_value = res
        q.first.return_value = res[0] if res else None
        q.scalar.return_value = 0
        return q

    db.query.side_effect = mock_query

    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("src.services.ap_decision_engine.find_duplicate_invoice", lambda *a, **k: None)
    monkeypatch.setattr("src.services.llm_matching_services.llm_evaluate_invoice_match", lambda *a, **k: None)

    # First run: partial receipt (5 accepted vs 10 billed) -> QUANTITY_VARIANCE -> needs_review
    run1 = matching.run_match_for_invoice(db, invoice_id, force=True)
    assert run1.result == "match_issues"
    assert invoice.status == "needs_review"

    # Now second receipt arrives with remaining 5 units (total accepted = 10)
    gr_line.accepted_qty = Decimal("10")
    gr_line.received_qty = Decimal("10")

    # Second run: now matches correctly -> ready_to_pay!
    run2 = matching.run_match_for_invoice(db, invoice_id, force=True)
    assert run2.result == "match_ok"
    assert invoice.status == "ready_to_pay"
    assert invoice.workflow_status == "match_approved"
