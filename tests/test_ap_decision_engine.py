from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services import ap_decision_engine as engine


def _fixture(monkeypatch, *, po_count=1, receipt=True, duplicate=False):
    vendor_id = "vendor-1"
    po_line = SimpleNamespace(
        id="pl-1",
        item_code="SKU-1",
        description="Widget",
        qty=Decimal(10),
        unit_price=Decimal(10),
        line_amount=Decimal(100),
    )
    po = SimpleNamespace(
        id="po-1",
        po_number="PO-1",
        vendor_id=vendor_id,
        currency="USD",
        tax_amount=Decimal(0),
        total_amount=Decimal(100),
    )
    gr = SimpleNamespace(id="gr-1")
    gr_line = SimpleNamespace(po_line_id="pl-1", accepted_qty=Decimal(10))
    line = SimpleNamespace(
        item_code="SKU-1",
        description="Widget",
        qty=Decimal(10),
        unit_price=Decimal(10),
        line_amount=Decimal(100),
        field_confidence={
            key: {"confidence": 85} for key in ("qty", "unit_price", "line_amount")
        },
    )
    invoice = SimpleNamespace(
        id="inv-1",
        po_number_raw="PO-1",
        po_id=None,
        vendor_id=vendor_id,
        invoice_no="INV-1",
        invoice_no_normalized="INV1",
        currency="USD",
        subtotal=Decimal(100),
        tax_amount=Decimal(0),
        total_amount=Decimal(100),
        field_confidence={key: {"confidence": 85} for key in engine.MATERIAL_FIELDS},
    )
    datasets = {
        PurchaseOrder: [po] * po_count,
        PurchaseOrderLine: [po_line],
        GoodsReceipt: [gr] if receipt else [],
        GoodsReceiptLine: [gr_line] if receipt else [],
        InvoiceLine: [line],
    }
    db = MagicMock()

    def query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.all.return_value = datasets.get(model, [])
        return q

    db.query.side_effect = query
    monkeypatch.setattr(
        engine,
        "find_duplicate_invoice",
        lambda *_a, **_k: object() if duplicate else None,
    )
    return db, invoice, po, line


def _codes(monkeypatch, mutate=lambda _i, _p, _l: None, **kwargs):
    db, invoice, po, line = _fixture(monkeypatch, **kwargs)
    mutate(invoice, po, line)
    return {
        item["code"] for item in engine.evaluate_invoice(db, invoice)["discrepancies"]
    }


@pytest.mark.parametrize(
    ("code", "mutate", "options"),
    [
        (
            "MISSING_PO",
            lambda invoice, purchase_order, line: setattr(
                invoice, "po_number_raw", None
            ),
            {},
        ),
        ("AMBIGUOUS_PO", lambda invoice, purchase_order, line: None, {"po_count": 2}),
        (
            "VENDOR_MISMATCH",
            lambda invoice, purchase_order, line: setattr(
                invoice, "vendor_id", "other"
            ),
            {},
        ),
        ("MISSING_GR", lambda invoice, purchase_order, line: None, {"receipt": False}),
        (
            "LINE_NOT_ON_PO",
            lambda invoice, purchase_order, line: setattr(line, "item_code", "other"),
            {},
        ),
        (
            "QUANTITY_VARIANCE",
            lambda invoice, purchase_order, line: setattr(line, "qty", Decimal(11)),
            {},
        ),
        (
            "PRICE_VARIANCE",
            lambda invoice, purchase_order, line: setattr(
                line, "unit_price", Decimal("10.21")
            ),
            {},
        ),
        (
            "LINE_TOTAL_VARIANCE",
            lambda invoice, purchase_order, line: setattr(
                line, "line_amount", Decimal(99)
            ),
            {},
        ),
        (
            "TAX_VARIANCE",
            lambda invoice, purchase_order, line: (
                setattr(invoice, "tax_amount", Decimal(1)),
                setattr(invoice, "total_amount", Decimal(101)),
            ),
            {},
        ),
        (
            "HEADER_TOTAL_VARIANCE",
            lambda invoice, purchase_order, line: setattr(
                invoice, "total_amount", Decimal(101)
            ),
            {},
        ),
        (
            "CURRENCY_MISMATCH",
            lambda invoice, purchase_order, line: setattr(invoice, "currency", "EUR"),
            {},
        ),
        (
            "POSSIBLE_DUPLICATE",
            lambda invoice, purchase_order, line: None,
            {"duplicate": True},
        ),
        (
            "LOW_CONFIDENCE",
            lambda invoice, purchase_order, line: invoice.field_confidence[
                "total"
            ].update(confidence=84),
            {},
        ),
    ],
)
def test_each_rule_reason_code(monkeypatch, code, mutate, options):
    assert code in _codes(monkeypatch, mutate, **options)


def test_ready_to_pay_requires_exact_two_percent_and_85_confidence(monkeypatch):
    db, invoice, _po, line = _fixture(monkeypatch)
    line.unit_price = Decimal("10.20")
    line.line_amount = Decimal("102")
    invoice.subtotal = Decimal("102")
    invoice.total_amount = Decimal("102")
    outcome = engine.evaluate_invoice(db, invoice)
    assert outcome["outcome"] == "ready_to_pay"


def test_multiple_discrepancies_are_kept(monkeypatch):
    def mutate(invoice, _po, line):
        line.qty = Decimal(12)
        line.unit_price = Decimal("10.50")
        line.line_amount = Decimal(120)
        invoice.currency = "EUR"

    codes = _codes(monkeypatch, mutate)
    assert {"QUANTITY_VARIANCE", "PRICE_VARIANCE", "CURRENCY_MISMATCH"} <= codes
