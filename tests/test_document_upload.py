import sys
import uuid
from io import BytesIO
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock
from zipfile import ZIP_DEFLATED, ZipFile

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import document_routers
from src.db.session_db import get_db
from src.models.document_models import Document
from src.models.user_models import User
from src.services import document_ingest_services

client = TestClient(app)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(*, extra_entries=None, document=b"<w:document/>"):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr("word/document.xml", document)
        for name, value in (extra_entries or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


def setup_db(monkeypatch, db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}
    monkeypatch.setattr(
        document_ingest_services, "upload_file", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        document_ingest_services, "delete_file", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(document_routers, "move_file", lambda *_: None)
    monkeypatch.setattr(
        document_routers,
        "_process_in_background",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        document_routers,
        "_process_document_with_retries",
        lambda *args, **kwargs: None,
    )


def teardown_db():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _request_document_file(monkeypatch, *, disposition=None, filename="invoice.pdf"):
    document_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        storage_path=f"documents/invoice/original/{filename}",
        original_filename=filename,
        mime="application/octet-stream",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = document
    setup_db(monkeypatch, db)
    monkeypatch.setattr(document_routers, "download_file", lambda _path: b"%PDF")
    params = {"disposition": disposition} if disposition else None
    try:
        return client.get(f"/api/documents/{document_id}/file", params=params)
    finally:
        teardown_db()


@pytest.mark.parametrize(
    ("disposition", "expected_disposition"),
    [(None, "attachment"), ("inline", "inline")],
)
def test_original_file_disposition_and_content_type(
    monkeypatch, disposition, expected_disposition
):
    response = _request_document_file(monkeypatch, disposition=disposition)

    assert response.status_code == 200
    assert response.content == b"%PDF"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith(
        f"{expected_disposition};"
    )
    assert response.headers["x-content-type-options"] == "nosniff"


def test_original_file_rejects_unknown_disposition(monkeypatch):
    response = _request_document_file(monkeypatch, disposition="preview")

    assert response.status_code == 422


def test_original_docx_file_uses_word_content_type(monkeypatch):
    response = _request_document_file(monkeypatch, filename="invoice.docx")

    assert response.status_code == 200
    assert response.headers["content-type"] == DOCX_MIME
    assert response.headers["content-disposition"].startswith("attachment;")


def test_upload_valid_pdf(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "invoice.pdf",
                    b"fake PDF content",
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    body = response.json()
    assert body["original_filename"] == "invoice.pdf"
    assert body["status"] == "received"
    assert "id" in body

    assert any(isinstance(call.args[0], Document) for call in db.add.call_args_list)
    db.commit.assert_called_once()


def test_upload_rejects_more_than_five_files_before_ingest(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)
    files = [
        ("file", (f"invoice-{index}.pdf", b"%PDF-1.4", "application/pdf"))
        for index in range(6)
    ]

    try:
        response = client.post("/api/documents/upload", files=files)
    finally:
        teardown_db()

    assert response.status_code == 422
    assert response.json()["detail"] == "You can upload up to 5 files at a time"
    db.add.assert_not_called()


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("invoice.pdf", b"PDF", "application/pdf"),
        ("invoice.docx", _docx_bytes(), DOCX_MIME),
    ],
)
def test_upload_extract_pipeline_matches_and_approves_without_manual_route(
    monkeypatch, filename, content, content_type
):
    from src.models.document_models import Document
    from src.models.goods_receipt_models import GoodsReceipt
    from src.models.invoice_match_models import InvoiceDiscrepancy, InvoiceMatch
    from src.models.invoice_models import InvoiceLine
    from src.models.match_run_models import MatchRun
    from src.models.notification_models import Notification
    from src.models.purchase_order_models import PurchaseOrderLine
    from src.services import invoice_matching_services

    owner_id = uuid.uuid4()
    po = SimpleNamespace(id=uuid.uuid4())
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        po_id=po.id,
        source_document_id=None,
        final_status=None,
        final_run_id=None,
        final_by=None,
        final_at=None,
        match_status="pending",
        invoice_no="INV-1",
        workflow_status="matching",
        status="processing",
        field_confidence={
            field: {"confidence": 95, "derived": False}
            for field in (
                "vendor",
                "invoice_number",
                "invoice_date",
                "due_date",
                "currency",
                "subtotal",
                "tax",
                "total",
                "order_number",
                "gl_code",
            )
        },
    )
    db = MagicMock()
    document = None
    document_query = MagicMock()
    document_query.filter_by.return_value.first.return_value = None
    locked_document_query = (
        document_query.filter.return_value.with_for_update.return_value
    )
    locked_document_query.first.side_effect = lambda: document
    empty_query = MagicMock()
    empty_query.filter.return_value.all.return_value = []
    empty_query.all.return_value = []
    previous_query = MagicMock()
    previous_query.filter.return_value.order_by.return_value.first.return_value = None
    max_query = MagicMock()
    max_query.filter.return_value.scalar.return_value = 0
    notification_query = MagicMock()
    notification_query.filter.return_value.all.return_value = []
    match_query_number = 0

    def query(model, *args):
        nonlocal match_query_number
        if model is Document:
            return document_query
        if model in {InvoiceLine, PurchaseOrderLine}:
            return empty_query
        if model is MatchRun:
            match_query_number += 1
            return previous_query
        if str(model).startswith("max("):
            return max_query
        if model is Notification:
            return notification_query
        from src.models.invoice_issue_models import InvoiceIssue
        from src.models.invoice_models import Invoice
        from src.models.user_models import User

        if model in {
            InvoiceIssue,
            InvoiceDiscrepancy,
            InvoiceMatch,
            GoodsReceipt,
            User,
            InvoiceIssue.owner_user_id,
            Invoice.owner_user_id,
        }:
            return empty_query
        raise AssertionError(f"Unexpected query model: {model}")

    db.query.side_effect = query
    db.get.side_effect = lambda model, _id: (
        invoice
        if model.__name__ == "Invoice"
        else po
        if model.__name__ == "PurchaseOrder"
        else document
    )

    def add(row):
        nonlocal document
        if isinstance(row, Document):
            document = row

    db.add.side_effect = add

    def flush():
        for call in db.add.call_args_list:
            row = call.args[0]
            if isinstance(row, MatchRun) and row.id is None:
                row.id = uuid.uuid4()

    db.flush.side_effect = flush
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    monkeypatch.setattr(
        document_ingest_services, "upload_file", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(document_routers, "download_file", lambda *_: b"PDF")
    routed_files = []
    monkeypatch.setattr(
        document_routers,
        "move_file",
        lambda source, destination: routed_files.append((source, destination)),
    )
    monkeypatch.setenv("USE_CELERY", "false")

    class FakeConverter:
        def convert(self, _path):
            return SimpleNamespace(
                document=SimpleNamespace(export_to_markdown=lambda: "Invoice INV-1")
            )

    fake_docling = ModuleType("docling")
    fake_converter = ModuleType("docling.document_converter")
    fake_converter.DocumentConverter = FakeConverter
    monkeypatch.setitem(sys.modules, "docling", fake_docling)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_converter)
    monkeypatch.setattr(document_routers, "classify_document", lambda _: "invoice")
    monkeypatch.setattr(
        document_routers,
        "extract_document",
        lambda *_: {
            "document_type": "invoice",
            "vendor": "Acme",
            "invoice_number": "INV-1",
            "invoice_date": "2026-10-01",
            "currency": "USD",
            "total": "10",
            "subtotal": "10",
            "tax": "0",
            "field_confidence": {
                field: {"confidence": 95, "derived": False}
                for field in (
                    "vendor",
                    "invoice_number",
                    "invoice_date",
                    "due_date",
                    "currency",
                    "subtotal",
                    "tax",
                    "total",
                    "order_number",
                    "gl_code",
                )
            },
            "line_items": [],
        },
    )

    def save_invoice(_db, saved_doc, _data):
        nonlocal document
        document = saved_doc
        return invoice

    monkeypatch.setattr(document_routers, "_save_invoice_from_extraction", save_invoice)
    monkeypatch.setattr(
        invoice_matching_services,
        "_invoice_state_fingerprint",
        lambda *_: "pipeline-state",
    )
    monkeypatch.setattr(
        invoice_matching_services,
        "evaluate_invoice",
        lambda *_args, **_kwargs: {
            "rule_set_version": "ap-match-v1",
            "outcome": "ready_to_pay",
            "discrepancies": [],
            "low_confidence_fields": [],
            "inputs_summary": {},
        },
    )

    def process_document(document_id):
        document_routers.parse_document(
            document_id=document_id,
            background_tasks=None,
            db=db,
            current_user={"sub": str(owner_id)},
        )

    monkeypatch.setattr(document_routers, "_process_in_background", process_document)
    monkeypatch.setattr(
        document_routers, "_process_document_with_retries", process_document
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": (filename, content, content_type)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert document.status == "extracted"
    assert len(routed_files) == 1
    assert routed_files[0][1].startswith("invoice/")
    assert invoice.final_status is None
    assert any(
        isinstance(call.args[0], MatchRun) and call.args[0].result == "match_ok"
        for call in db.add.call_args_list
    )


def test_upload_accepts_valid_docx(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.docx", _docx_bytes(), DOCX_MIME)},
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert response.json()["status"] == "received"
    assert response.json()["original_filename"] == "invoice.docx"
    assert response.json()["id"]
    stored = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Document)
    )
    assert stored.mime == DOCX_MIME


@pytest.mark.parametrize(
    ("filename", "payload", "mime", "expected_message"),
    [
        (
            "invoice.docx",
            b"not a zip file",
            DOCX_MIME,
            "Invalid Word document; upload a valid .docx file",
        ),
        ("invoice.docm", _docx_bytes(), DOCX_MIME, "Unsupported file type"),
        ("invoice.doc", _docx_bytes(), DOCX_MIME, "Unsupported file type"),
        ("template.dotm", _docx_bytes(), DOCX_MIME, "Unsupported file type"),
        (
            "invoice.docx",
            _docx_bytes(extra_entries={"word/vbaProject.bin": b"macro"}),
            DOCX_MIME,
            "Invalid Word document; upload a valid .docx file",
        ),
        (
            "invoice.docx",
            _docx_bytes(),
            "application/zip",
            "Invalid Word document; upload a valid .docx file",
        ),
    ],
)
def test_upload_rejects_invalid_or_macro_enabled_docx(
    monkeypatch, filename, payload, mime, expected_message
):
    db = MagicMock()
    setup_db(monkeypatch, db)
    try:
        response = client.post(
            "/api/documents/upload", files={"file": (filename, payload, mime)}
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert response.json()["id"] is None
    assert response.json()["message"] == expected_message
    db.add.assert_not_called()


def test_docx_zip_bomb_entry_count_is_rejected():
    from src.services.docx_validation_services import is_valid_docx

    bomb = _docx_bytes(
        extra_entries={f"word/extra-{index}.xml": b"x" for index in range(1000)}
    )
    assert not is_valid_docx("invoice.docx", DOCX_MIME, bomb)


def test_docx_zip_bomb_uncompressed_size_is_rejected(monkeypatch):
    from src.services import docx_validation_services

    monkeypatch.setattr(docx_validation_services, "DOCX_MAX_UNCOMPRESSED_BYTES", 10)
    payload = _docx_bytes(extra_entries={"word/expanded.xml": b"x" * 11})
    assert not docx_validation_services.is_valid_docx(
        "invoice.docx", DOCX_MIME, payload
    )


def test_valid_docx_duplicate_hash_is_blocked(monkeypatch):
    existing_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
        id=existing_id
    )
    setup_db(monkeypatch, db)
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.docx", _docx_bytes(), DOCX_MIME)},
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert response.json()["status"] == "duplicate_blocked"
    assert response.json()["id"] == str(existing_id)
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_upload_rejects_unsupported_extension(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "malware.exe",
                    b"example content",
                    "application/octet-stream",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert response.json()["results"][0]["message"] == "Unsupported file type"
    db.add.assert_not_called()


def test_upload_rejects_empty_file(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "empty.pdf",
                    b"",
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert response.json()["results"][0]["message"] == "Uploaded file is empty"
    db.add.assert_not_called()


def test_upload_rejects_file_over_20_mb(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)

    oversized_content = b"x" * (20 * 1024 * 1024 + 1)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "large.pdf",
                    oversized_content,
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert response.json()["results"][0]["message"] == "File exceeds 20 MB limit"
    db.add.assert_not_called()


def test_upload_blocks_duplicate_file(monkeypatch):
    existing_id = uuid.uuid4()

    existing_document = SimpleNamespace(
        id=existing_id,
        status="received",
    )

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing_document

    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "invoice.pdf",
                    b"same invoice content",
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    body = response.json()
    assert body["id"] == str(existing_id)
    assert body["status"] == "duplicate_blocked"
    assert body["message"] == "This file has already been uploaded"

    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_upload_triggers_auto_processing(monkeypatch):
    # This test covers the in-process fallback. Docker test stacks can enable
    # Celery, where dispatch queues process_document.delay instead.
    monkeypatch.setenv("USE_CELERY", "false")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)

    started = []
    monkeypatch.setattr(
        document_routers,
        "_process_document_with_retries",
        lambda document_id: started.append(document_id),
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")},
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert started == [uuid.UUID(response.json()["id"])]


def test_uuid_subject_upload_enqueues_document(monkeypatch, user_stub):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)
    app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")
    owner_id = user_stub.id
    db.get.side_effect = lambda model, _id: user_stub if model is User else None
    token = jwt.encode(
        {"sub": str(owner_id), "type": "access"},
        "test-secret-for-authentication-tests",
        algorithm="HS256",
    )
    queued = []
    monkeypatch.setattr(
        document_ingest_services,
        "dispatch_document_processing",
        lambda _tasks, document_id, _fallback: queued.append(document_id),
    )

    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    assert queued == [uuid.UUID(response.json()["id"])]
    assert db.add.call_args.args[0].created_by == owner_id


def test_background_processing_uses_stored_uploader_identity(monkeypatch):
    owner_id = uuid.uuid4()
    document = SimpleNamespace(id=uuid.uuid4(), status="received", created_by=owner_id)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = document
    monkeypatch.setattr(document_routers, "SessionLocal", lambda: db)
    processed = []
    monkeypatch.setattr(
        document_routers,
        "parse_document",
        lambda **kwargs: processed.append(kwargs),
    )

    document_routers._process_in_background(document.id)

    assert processed == [
        {
            "document_id": document.id,
            "background_tasks": None,
            "db": db,
            "current_user": {"sub": str(owner_id)},
        }
    ]
    db.close.assert_called_once()


def test_duplicate_upload_does_not_trigger_processing(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
        id=uuid.uuid4()
    )
    setup_db(monkeypatch, db)

    started = []
    monkeypatch.setattr(
        document_routers,
        "_process_in_background",
        lambda document_id: started.append(document_id),
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")},
        )
    finally:
        teardown_db()

    assert response.json()["status"] == "duplicate_blocked"
    assert started == []


def test_multi_upload_returns_success_duplicate_and_corrupt_results(monkeypatch):

    stored = []
    db = MagicMock()
    query = MagicMock()
    query.filter_by.side_effect = lambda **kwargs: SimpleNamespace(
        first=lambda: next(
            (
                d
                for d in stored
                if all(getattr(d, k, None) == v for k, v in kwargs.items())
            ),
            None,
        )
    )
    db.query.return_value = query
    db.add.side_effect = lambda row: stored.append(row)
    setup_db(monkeypatch, db)

    def fail_corrupt(_key, content, _content_type):
        if content == b"corrupt":
            raise ValueError("corrupt file")

    monkeypatch.setattr(document_ingest_services, "upload_file", fail_corrupt)
    try:
        response = client.post(
            "/api/documents/upload",
            files=[
                ("file", ("ok.pdf", b"valid", "application/pdf")),
                ("file", ("duplicate.pdf", b"valid", "application/pdf")),
                ("file", ("bad.pdf", b"corrupt", "application/pdf")),
            ],
        )
    finally:
        teardown_db()

    assert response.status_code == 202
    results = response.json()["results"]
    assert len(results) == 3
    assert [row["status"] for row in results] == [
        "received",
        "duplicate_blocked",
        "failed",
    ]
    assert [row["result"] for row in results] == ["accepted", "duplicate", "failed"]
    assert results[0]["document_id"] == results[1]["document_id"]


def test_multi_upload_marks_non_bill_result_as_ignored(monkeypatch):
    from src.services.document_ingest_services import IngestResult

    db = MagicMock()
    setup_db(monkeypatch, db)
    document_id = uuid.uuid4()
    monkeypatch.setattr(
        document_routers,
        "ingest_file",
        lambda *_args, **_kwargs: IngestResult(
            "other", document_id, "No bill attachment found"
        ),
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("notice.pdf", b"pdf bytes", "application/pdf")},
        )
    finally:
        teardown_db()

    result = response.json()["results"][0]
    assert result["result"] == "ignored non-bill"
    assert result["status"] == "other"
    assert result["message"] == "No bill attachment found"
