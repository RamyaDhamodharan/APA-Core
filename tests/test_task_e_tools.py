import pytest

from scripts import evaluate, seed_import
from src.services import llm_pipeline_services


def test_seed_import_reads_csv_using_cli_selected_path(tmp_path):
    path = tmp_path / "vendors.csv"
    path.write_text("name,tax_id\nSample Vendor,TEST-1\n", encoding="utf-8")
    assert seed_import._rows(path, "auto") == [
        {"name": "Sample Vendor", "tax_id": "TEST-1"}
    ]


def test_vendor_import_is_idempotent_and_reports_counts():
    class FakeQuery:
        def __init__(self, db):
            self.db = db

        def filter(self, *_args):
            return self

        def first(self):
            return next(
                (
                    row
                    for row in self.db.vendors
                    if row.name.casefold() == "sample vendor"
                ),
                None,
            )

    class FakeDB:
        vendors = []

        def query(self, *_args):
            return FakeQuery(self)

        def add(self, row):
            self.vendors.append(row)

        def commit(self):
            pass

        def rollback(self):
            pass

    db = FakeDB()
    rows = [{"name": "Sample Vendor", "tax_id": "TEST-1"}]
    assert seed_import.import_vendors(db, rows)["created"] == 1
    summary = seed_import.import_vendors(db, rows)
    assert summary["skipped"] == 1
    assert summary["errors"] == []


def test_purchase_order_and_goods_receipt_import_build_lines():
    from src.models.goods_receipt_models import GoodsReceipt
    from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine

    vendor = type("Vendor", (), {"id": __import__("uuid").uuid4()})()
    po_line = PurchaseOrderLine(
        id=__import__("uuid").uuid4(),
        item_code="ITEM-1",
        description="Sample",
        uom="ea",
    )
    po = PurchaseOrder(
        id=__import__("uuid").uuid4(),
        po_number="PO-1",
        vendor_id=vendor.id,
        is_current=True,
        lines=[po_line],
    )

    class Query:
        def __init__(self, result):
            self.result = result

        def filter(self, *_args):
            return self

        def filter_by(self, **_kwargs):
            return self

        def first(self):
            return self.result

    class FakeDB:
        def __init__(self, results=()):
            self.results = iter(results)
            self.added = []

        def query(self, *_args):
            return Query(next(self.results))

        def add(self, row):
            self.added.append(row)

        def commit(self):
            pass

        def rollback(self):
            pass

    po_db = FakeDB([None, vendor])
    po_result = seed_import.import_purchase_orders(
        po_db,
        [
            {
                "po_number": "PO-2",
                "vendor": "Sample Vendor",
                "item_code": "ITEM-2",
                "qty": "10",
                "unit_price": "5",
                "currency": "INR",
            }
        ],
    )
    imported_po = po_db.added[0]
    assert po_result["created"] == 1
    assert isinstance(imported_po, PurchaseOrder)
    assert imported_po.total_amount == 50
    assert imported_po.lines[0].line_amount == 50

    receipt_db = FakeDB([None, po])
    receipt_result = seed_import.import_goods_receipts(
        receipt_db,
        [
            {
                "gr_number": "GR-1",
                "po_number": "PO-1",
                "receipt_date": "2026-10-01",
                "item_code": "ITEM-1",
                "received_qty": "8",
                "accepted_qty": "7",
                "rejected_qty": "1",
            }
        ],
    )
    assert receipt_result["errors"] == [], receipt_result
    imported_receipt = receipt_db.added[0]
    assert receipt_result["created"] == 1
    assert isinstance(imported_receipt, GoodsReceipt)
    assert imported_receipt.lines[0].po_line_id == po_line.id


def test_evaluation_report_scores_fields_match_and_duplicate_cases():
    expected = {
        "bill.pdf": {
            "fields": {"invoice_number": "INV-1", "total": "100"},
            "match_result": "match_ok",
            "duplicate": False,
        },
        "duplicate.pdf": {"duplicate": True},
    }
    scores = evaluate._score(
        [
            {
                "filename": "bill.pdf",
                "extracted_json": {"invoice_number": "INV-1", "total": "100"},
                "match_result": "match_ok",
            },
            {"filename": "duplicate.pdf", "result": "accepted"},
        ],
        expected,
    )
    assert scores["field_accuracy"] == 1
    assert scores["match_result_accuracy"] == 1
    assert scores["missed_duplicates"] == 1
    assert scores["match_confusion"] == {"match_ok": {"match_ok": 1}}


def test_quality_metrics_cover_field_amount_outcome_and_confidence_cases():
    expected = {
        "one.pdf": {
            "fields": {
                "vendor": "Acme",
                "invoice_number": "INV-1",
                "invoice_date": "2026-01-02",
                "total": "100.00",
            },
            "match_result": "matched",
        },
        "two.png": {"fields": {"invoice_number": "INV-2"}, "match_result": "no_match"},
    }
    metrics = evaluate.score_quality(
        [
            {
                "filename": "one.pdf",
                "extracted_json": {
                    "vendor": "Acme",
                    "invoice_number": "INV-1",
                    "invoice_date": "2026-01-02",
                    "total_amount": "100.009",
                },
                "match_result": "matched",
                "confidence_band": "high",
            },
            {
                "filename": "two.png",
                "extracted_json": {"invoice_number": "OTHER"},
                "match_result": "needs_review",
                "confidence_band": "low",
            },
        ],
        expected,
    )
    assert metrics["fields"]["total"]["accuracy"] == 1
    assert metrics["fields"]["invoice_number"]["accuracy"] == 0.5
    assert metrics["field_accuracy"] == 4 / 5
    assert metrics["match_accuracy"] == 0.5
    assert metrics["match_confusion"]["no_match"]["needs_review"] == 1
    assert metrics["no_match"] == {"precision": None, "recall": 0.0}
    assert metrics["needs_review"] == {"precision": 0.0, "recall": None}
    assert metrics["confidence_calibration"]["high"]["accuracy"] == 1
    assert metrics["confidence_calibration"]["low"]["accuracy"] == 0
    assert "two.png" in metrics["mismatches"]


def test_evaluator_safety_requires_confirmation_and_exact_database_allowlist():
    url = "postgresql+psycopg:///eval_core"
    with pytest.raises(ValueError, match="--confirm"):
        evaluate._validate_eval_safety(False, url, "eval_core")
    with pytest.raises(ValueError, match="EVAL_ALLOWED_DATABASES"):
        evaluate._validate_eval_safety(True, url, "core")
    assert evaluate._validate_eval_safety(True, url, "eval_core,other") == "eval_core"


def test_evaluation_loads_csv_answer_key(tmp_path):
    key = tmp_path / "key.csv"
    key.write_text(
        "filename,vendor,invoice_number,invoice_date,due_date,currency,subtotal,"
        "tax,total,po_number,expected_match_outcome\n"
        "sample.pdf,Acme,INV-1,,,,,,,,matched\n",
        encoding="utf-8",
    )
    row = evaluate._load_answer_rows(key)["sample.pdf"]
    assert row["fields"]["vendor"] == "Acme"
    assert row["fields"]["invoice_number"] == "INV-1"
    assert row["match_result"] == "matched"


def test_evaluator_identifies_email_duplicate_without_a_new_document(
    monkeypatch, tmp_path
):
    message = tmp_path / "duplicate.eml"
    message.write_bytes(
        b"From: sender@example.test\r\nTo: ap@example.test\r\n"
        b"Subject: duplicate\r\nMessage-ID: <duplicate@example.test>\r\n\r\n"
    )

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def send_message(self, _message):
            pass

    class Response:
        def raise_for_status(self):
            pass

        @staticmethod
        def json():
            return {"duplicates": 1}

    class Client:
        @staticmethod
        def post(_path):
            return Response()

    monkeypatch.setattr(evaluate.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(
        evaluate,
        "_find_new_email_document",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("no new document")),
    )
    args = type(
        "Args", (), {"smtp_host": "localhost", "smtp_port": 3025, "timeout": 1}
    )()
    assert (
        evaluate._submit_email(Client(), message, args, set())["result"] == "duplicate"
    )


def test_stub_provider_is_confined_to_e2e(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("APP_ENV", "production")
    try:
        llm_pipeline_services.classify_document("anything")
    except RuntimeError as exc:
        assert "requires APP_ENV=e2e" in str(exc)
    else:
        raise AssertionError("Stub provider must be rejected outside APP_ENV=e2e")


def test_stub_provider_runs_with_e2e_json_payload(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("APP_ENV", "e2e")
    text = (
        "GW_AP_E2E_JSON_BEGINeyJkb2N1bWVudF90eXBlIjoiaW52b2ljZSIs"
        "InRvdGFsIjoiMTAwIn0=GW_AP_E2E_JSON_END"
    )
    assert llm_pipeline_services.classify_document(text) == "invoice"
    assert llm_pipeline_services.extract_document(text, "invoice")["total"] == "100"
