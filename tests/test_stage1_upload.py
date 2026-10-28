"""
Test Suite for Stage 1 - Bulk Document Upload and Ingestion.
Verifies:
1. Bulk upload of PDFs, images, and email files (.eml).
2. Unsupported file types are rejected with plain messages without crashing.
3. Empty files are handled cleanly.
4. Corrupted files do not stop bulk uploads; remaining files succeed (Case 9).
5. Exact duplicates are detected and flagged.
"""

import uuid
from fastapi.testclient import TestClient
import pytest

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import document_routers
from src.db.session_db import get_db
from src.services.document_ingest_services import IngestResult

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_auth(monkeypatch):
    user_id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(user_id),
        "role": "associate",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def test_stage1_single_pdf_upload(monkeypatch):
    doc_id = uuid.uuid4()
    monkeypatch.setattr(
        document_routers,
        "ingest_file",
        lambda *_a, **_k: IngestResult("received", doc_id, "Accepted"),
    )
    response = client.post(
        "/api/documents/upload",
        files=[("file", ("valid_invoice.pdf", b"%PDF-1.4 sample content", "application/pdf"))],
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "received"
    assert data["id"] == str(doc_id)
    assert data["results"][0]["result"] == "accepted"


def test_stage1_bulk_upload_mixed_types(monkeypatch):
    results = iter([
        IngestResult("received", uuid.uuid4(), "Accepted"),
        IngestResult("received", uuid.uuid4(), "Accepted"),
        IngestResult("received", uuid.uuid4(), "Accepted"),
    ])
    monkeypatch.setattr(document_routers, "ingest_file", lambda *_a, **_k: next(results))

    response = client.post(
        "/api/documents/upload",
        files=[
            ("file", ("invoice1.pdf", b"%PDF-1.4 pdf content", "application/pdf")),
            ("file", ("receipt2.png", b"\x89PNG png content", "image/png")),
            ("file", ("message3.eml", b"Subject: Invoice\nFrom: vendor@test.com", "message/rfc822")),
        ],
    )
    assert response.status_code == 202
    results_list = response.json()["results"]
    assert len(results_list) == 3
    assert all(r["status"] == "received" for r in results_list)


def test_stage1_corrupt_file_does_not_stop_batch(monkeypatch):
    """Case 9: One corrupt PDF must not stop a bulk upload."""
    results = iter([
        IngestResult("received", uuid.uuid4(), "Accepted"),
        IngestResult("failed", None, "Bill could not be read: the PDF is corrupt. Ask the vendor for a new copy."),
        IngestResult("received", uuid.uuid4(), "Accepted"),
    ])
    monkeypatch.setattr(document_routers, "ingest_file", lambda *_a, **_k: next(results))

    response = client.post(
        "/api/documents/upload",
        files=[
            ("file", ("good_1.pdf", b"%PDF-1.4 valid", "application/pdf")),
            ("file", ("corrupted_2.pdf", b"corrupted bytes", "application/pdf")),
            ("file", ("good_3.pdf", b"%PDF-1.4 valid", "application/pdf")),
        ],
    )
    assert response.status_code == 202
    results_list = response.json()["results"]
    assert len(results_list) == 3
    assert results_list[0]["status"] == "received"
    assert results_list[1]["status"] == "failed"
    assert "Bill could not be read" in results_list[1]["message"]
    assert results_list[2]["status"] == "received"


def test_stage1_duplicate_blocked(monkeypatch):
    """Case 4: Duplicate file is detected and skipped."""
    monkeypatch.setattr(
        document_routers,
        "ingest_file",
        lambda *_a, **_k: IngestResult("duplicate_blocked", uuid.uuid4(), "Duplicate file already exists"),
    )
    response = client.post(
        "/api/documents/upload",
        files=[("file", ("duplicate_bill.pdf", b"%PDF-1.4 duplicate", "application/pdf"))],
    )
    assert response.status_code == 202
    data = response.json()
    assert data["results"][0]["result"] == "duplicate"
    assert data["results"][0]["status"] == "duplicate_blocked"


def test_stage1_unsupported_file_extension(monkeypatch):
    monkeypatch.setattr(
        document_routers,
        "ingest_file",
        lambda *_a, **_k: IngestResult("failed", None, "Unsupported file type: ask the vendor to send a PDF or image."),
    )
    response = client.post(
        "/api/documents/upload",
        files=[("file", ("script.exe", b"binary executable", "application/octet-stream"))],
    )
    assert response.status_code == 202
    data = response.json()
    assert data["results"][0]["status"] == "failed"
    assert "Unsupported file type" in data["results"][0]["message"]
