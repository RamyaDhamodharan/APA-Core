import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.api.rest.routers import document_routers
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.services import procurement_backfill_services


def _query(*, first=None, all_rows=None):
    query = MagicMock()
    query.filter.return_value = query
    query.filter_by.return_value = query
    query.with_for_update.return_value = query
    query.order_by.return_value = query
    query.first.return_value = first
    query.all.return_value = all_rows or []
    return query


def test_partial_goods_receipt_persists_and_retry_is_idempotent():
    receipt = None
    query_results = [(None, None), (None, None), (None, [])]
    db = MagicMock()

    def query_model(_model):
        first, all_rows = query_results.pop(0)
        return _query(first=first, all_rows=all_rows)

    db.query.side_effect = query_model
    document = SimpleNamespace(id=uuid.uuid4(), created_by=None)
    data = {
        "gr_number": "GR-PARTIAL-1",
        "line_items": [
            {
                "item_code": "SKU-1",
                "received_qty": "4",
                "field_confidence": {"received_qty": {"confidence": 60}},
            }
        ],
    }

    document_routers._save_goods_receipt_from_extraction(db, document, data, None)
    receipt = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], GoodsReceipt)
    )
    assert receipt.gr_number == "GR-PARTIAL-1"
    assert receipt.po_id is None
    assert receipt.vendor_id is None
    assert receipt.needs_verification is True
    assert receipt.lines[0].received_qty == Decimal("4")
    assert receipt.lines[0].accepted_qty is None
    assert receipt.lines[0].field_confidence["received_qty"]["confidence"] == 60

    query_results.extend([(receipt, None), (None, None), (None, [])])
    document_routers._save_goods_receipt_from_extraction(db, document, data, None)
    assert (
        sum(isinstance(call.args[0], GoodsReceipt) for call in db.add.call_args_list)
        == 1
    )


def test_email_document_uses_the_same_automatic_parse_worker(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(),
        status="received",
        source="email",
        created_by=None,
    )
    db = MagicMock()
    db.query.return_value = _query(first=document)
    monkeypatch.setattr(document_routers, "SessionLocal", lambda: db)
    parse_calls = []
    monkeypatch.setattr(
        document_routers,
        "parse_document",
        lambda **kwargs: parse_calls.append(kwargs),
    )

    document_routers._process_in_background(document.id)

    assert parse_calls[0]["document_id"] == document.id
    assert parse_calls[0]["db"] is db
    assert parse_calls[0]["current_user"] == {
        "sub": "system-worker",
        "worker": True,
    }
    db.close.assert_called_once()


def test_late_goods_receipt_forces_saved_invoice_rematch(monkeypatch):
    vendor_id = uuid.uuid4()
    po = SimpleNamespace(id=uuid.uuid4(), po_number="PO-LATE", vendor_id=vendor_id)
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        po_number_raw="PO-LATE",
        match_status="waiting_for_po",
        final_run_id=None,
        po_id=po.id,
        is_current=True,
        updated_by=None,
        created_by=None,
    )
    query_results = [
        (None, None),
        (None, None),
        (None, [po]),
        (None, []),
        (None, [invoice]),
    ]
    db = MagicMock()

    def query_model(_model):
        first, all_rows = query_results.pop(0)
        return _query(first=first, all_rows=all_rows)

    db.query.side_effect = query_model
    rematches = []
    monkeypatch.setattr(
        document_routers,
        "run_match_for_invoice",
        lambda _db, invoice_id, _actor, **options: rematches.append(
            (invoice_id, options)
        ),
    )

    document_routers._save_goods_receipt_from_extraction(
        db,
        SimpleNamespace(id=uuid.uuid4(), created_by=None),
        {
            "gr_number": "GR-LATE",
            "order_number": "PO-LATE",
            "line_items": [
                {
                    "item_code": "SKU-1",
                    "received_qty": "2",
                    "accepted_qty": "2",
                    "rejected_qty": "0",
                }
            ],
        },
        SimpleNamespace(id=vendor_id),
    )
    assert rematches == [
        (
            invoice.id,
            {
                "force": True,
                "idempotent": True,
                "trigger": "dependency_arrived",
            },
        )
    ]


def test_procurement_backfill_is_idempotent(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="po",
        status="needs_data_review",
        extracted_json={"po_number": "PO-BACKFILL", "vendor": "Known Vendor"},
        failure_message="Some document details need review.",
    )
    existing = SimpleNamespace(id=uuid.uuid4())
    db = MagicMock()
    db.query.side_effect = lambda model: (
        _query(all_rows=[document]) if model is Document else _query(first=existing)
    )
    monkeypatch.setattr(
        "src.services.llm_pipeline_services.validate_extracted_data",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        "src.services.procurement_backfill_services.resolve_vendor",
        lambda *_args, **_kwargs: SimpleNamespace(id=uuid.uuid4()),
    )
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._find_duplicate_purchase_order",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._purchase_order_persistence_issues",
        lambda *_args: [],
    )
    save = MagicMock(side_effect=AssertionError("existing row must not be recreated"))
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._save_purchase_order_from_extraction",
        save,
    )

    first = procurement_backfill_services.backfill_procurement_documents(db)
    second = procurement_backfill_services.backfill_procurement_documents(db)
    assert first["failed"] == 0, (first, db.query.call_args_list, save.call_args_list)
    assert first["already_present"] == 1, first
    assert second["already_present"] == 1
    assert first["created"] == second["created"] == 0
    save.assert_not_called()
    assert any(isinstance(call.args[0], AuditLog) for call in db.add.call_args_list)
