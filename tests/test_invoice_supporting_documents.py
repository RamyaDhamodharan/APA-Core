import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import invoice_supporting_document_routers as support_api
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.invoice_models import Invoice
from src.models.invoice_supporting_document_models import InvoiceSupportingDocument

client = TestClient(app)


def setup_api(db, *, role="associate"):
    user_id = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(user_id),
        "role": role,
    }
    return user_id


def teardown_api():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def fake_invoice_query(invoice):
    query = MagicMock()
    query.filter.return_value.first.return_value = invoice
    return query


def test_upload_to_approved_invoice_stores_file_without_processing(monkeypatch):
    invoice_id = uuid.uuid4()
    invoice = SimpleNamespace(id=invoice_id, final_status="approved")
    invoice_query = fake_invoice_query(invoice)
    supporting_query = MagicMock()
    supporting_query.filter.return_value.first.return_value = None
    db = MagicMock()
    db.query.side_effect = lambda model, *args: (
        invoice_query if model is Invoice else supporting_query
    )
    user_id = setup_api(db)
    stored = []
    monkeypatch.setattr(
        support_api,
        "upload_file",
        lambda key, data, mime: stored.append((key, data, mime)),
    )
    monkeypatch.setattr(
        "src.services.document_ingest_services.ingest_file",
        lambda *args, **kwargs: pytest.fail("supporting file entered ingestion"),
        raising=False,
    )
    try:
        response = client.post(
            f"/api/invoices/{invoice_id}/supporting-documents",
            files=[
                ("files", ("delivery.pdf", b"%PDF-1.7\npayload", "application/pdf"))
            ],
            data={"category": "delivery_note", "description": "Signed copy"},
        )
    finally:
        teardown_api()
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "uploaded"
    assert stored[0][0].startswith(
        f"supporting/{datetime.now(UTC):%Y/%m}/{invoice_id}/"
    )
    assert stored[0][0].endswith("_delivery.pdf")
    record = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], InvoiceSupportingDocument)
    )
    assert record.uploaded_by == user_id
    audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert audit.action == "supporting_document_upload"
    assert audit.entity_id == invoice_id


def test_duplicate_hash_is_blocked(monkeypatch):
    invoice_id = uuid.uuid4()
    invoice_query = fake_invoice_query(SimpleNamespace(id=invoice_id))
    supporting_query = MagicMock()
    supporting_query.filter.return_value.first.return_value = SimpleNamespace(
        id=uuid.uuid4()
    )
    db = MagicMock()
    db.query.side_effect = lambda model, *args: (
        invoice_query if model is Invoice else supporting_query
    )
    setup_api(db)
    monkeypatch.setattr(
        support_api, "upload_file", lambda *_: pytest.fail("duplicate was stored")
    )
    try:
        response = client.post(
            f"/api/invoices/{invoice_id}/supporting-documents",
            files=[("files", ("proof.pdf", b"%PDF-1.7 same", "application/pdf"))],
            data={"category": "delivery_note"},
        )
    finally:
        teardown_api()
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "duplicate_blocked"


def test_bad_magic_bytes_are_rejected_per_file():
    invoice_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value = fake_invoice_query(SimpleNamespace(id=invoice_id))
    setup_api(db)
    try:
        response = client.post(
            f"/api/invoices/{invoice_id}/supporting-documents",
            files=[("files", ("fake.pdf", b"not a pdf", "application/pdf"))],
            data={"category": "other"},
        )
    finally:
        teardown_api()
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "rejected"


def test_oversized_file_is_rejected(monkeypatch):
    invoice_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value = fake_invoice_query(SimpleNamespace(id=invoice_id))
    setup_api(db)
    monkeypatch.setenv("INVOICE_SUPPORTING_MAX_FILE_MB", "1")
    try:
        response = client.post(
            f"/api/invoices/{invoice_id}/supporting-documents",
            files=[
                (
                    "files",
                    ("large.pdf", b"%PDF-" + b"x" * (1024 * 1024), "application/pdf"),
                )
            ],
            data={"category": "other"},
        )
    finally:
        teardown_api()
    assert response.status_code == 200
    assert "exceeds" in response.json()["results"][0]["message"]


def test_missing_invoice_returns_404():
    db = MagicMock()
    db.query.return_value = fake_invoice_query(None)
    setup_api(db)
    try:
        response = client.post(
            f"/api/invoices/{uuid.uuid4()}/supporting-documents",
            files=[("files", ("proof.pdf", b"%PDF-1.7 x", "application/pdf"))],
            data={"category": "other"},
        )
    finally:
        teardown_api()
    assert response.status_code == 404


def test_list_hides_deleted_documents_for_non_admin():
    invoice_id = uuid.uuid4()
    invoice_query = fake_invoice_query(SimpleNamespace(id=invoice_id))
    supporting_query = MagicMock()
    supporting_query.outerjoin.return_value = supporting_query
    supporting_query.filter.return_value = supporting_query
    supporting_query.order_by.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = lambda model, *args: (
        invoice_query if model is Invoice else supporting_query
    )
    setup_api(db)
    try:
        response = client.get(f"/api/invoices/{invoice_id}/supporting-documents")
    finally:
        teardown_api()
    assert response.status_code == 200
    assert response.json() == []
    assert len(supporting_query.filter.call_args.args) == 1


def test_include_deleted_is_admin_only():
    invoice_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value = fake_invoice_query(SimpleNamespace(id=invoice_id))
    setup_api(db)
    try:
        response = client.get(
            f"/api/invoices/{invoice_id}/supporting-documents?include_deleted=true"
        )
    finally:
        teardown_api()
    assert response.status_code == 403


def test_download_records_view_audit(monkeypatch):
    invoice_id = uuid.uuid4()
    document_id = uuid.uuid4()
    invoice_query = fake_invoice_query(SimpleNamespace(id=invoice_id))
    document = SimpleNamespace(
        id=document_id,
        invoice_id=invoice_id,
        file_name="note.txt",
        content_type="text/plain",
        storage_key="supporting/2026/10/invoice/note.txt",
        category="approval_email",
    )
    document_query = MagicMock()
    document_query.filter.return_value.first.return_value = document
    db = MagicMock()
    db.query.side_effect = [invoice_query, document_query]
    setup_api(db)
    monkeypatch.setattr(support_api, "download_file", lambda _: b"approval")
    try:
        response = client.get(
            f"/api/invoices/{invoice_id}/supporting-documents/{document_id}/file"
        )
    finally:
        teardown_api()
    assert response.status_code == 200
    assert response.content == b"approval"
    event = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert event.action == "supporting_document_viewed"
    assert event.result == "success"


def test_delete_requires_reason_and_soft_delete_keeps_object(monkeypatch):
    invoice_id = uuid.uuid4()
    document_id = uuid.uuid4()
    invoice_query = fake_invoice_query(SimpleNamespace(id=invoice_id))
    document = SimpleNamespace(
        id=document_id,
        invoice_id=invoice_id,
        file_name="contract.txt",
        category="contract",
        deleted_at=None,
        deleted_by=None,
        deleted_reason=None,
    )
    supporting_query = MagicMock()
    locked_query = supporting_query.filter.return_value.with_for_update.return_value
    locked_query.first.return_value = document
    db = MagicMock()
    db.query.side_effect = [invoice_query, supporting_query]
    setup_api(db)
    monkeypatch.setattr(
        support_api, "delete_file", lambda *_: pytest.fail("soft delete removed object")
    )
    try:
        missing_reason = client.request(
            "DELETE",
            f"/api/invoices/{invoice_id}/supporting-documents/{document_id}",
            json={"reason": "  "},
        )
    finally:
        teardown_api()
    assert missing_reason.status_code == 422

    db.query.side_effect = [invoice_query, supporting_query]
    setup_api(db)
    try:
        response = client.request(
            "DELETE",
            f"/api/invoices/{invoice_id}/supporting-documents/{document_id}",
            json={"reason": "Uploaded in error"},
        )
    finally:
        teardown_api()
    assert response.status_code == 200
    assert document.deleted_at is not None
    assert document.deleted_reason == "Uploaded in error"
    assert any(
        isinstance(call.args[0], AuditLog)
        and call.args[0].action == "supporting_document_deleted"
        for call in db.add.call_args_list
    )
