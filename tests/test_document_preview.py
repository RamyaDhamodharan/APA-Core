import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.services import document_preview_services as previews

client = TestClient(app)


def _document(filename="invoice.docx"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        original_filename=filename,
        storage_path="documents/source.docx",
        file_hash="a" * 64,
        sha256=None,
    )


def test_docx_preview_converts_and_caches(monkeypatch):
    document = _document()
    uploaded = []
    monkeypatch.setattr(previews, "file_exists", lambda _key: False)
    monkeypatch.setattr(previews, "download_file", lambda _key: b"docx")
    monkeypatch.setattr(previews, "_convert_docx", lambda *_args: b"%PDF-1.7")
    monkeypatch.setattr(previews, "upload_file", lambda *args: uploaded.append(args))

    assert previews.get_docx_preview(document) == b"%PDF-1.7"
    assert uploaded[0][0].startswith("derived/previews/")
    assert uploaded[0][2] == "application/pdf"


def test_docx_preview_cache_hit_skips_converter(monkeypatch):
    document = _document()
    monkeypatch.setattr(previews, "file_exists", lambda _key: True)
    monkeypatch.setattr(previews, "download_file", lambda _key: b"%PDF-CACHED")
    converter = MagicMock(side_effect=AssertionError("converter should not run"))
    monkeypatch.setattr(previews, "_convert_docx", converter)

    assert previews.get_docx_preview(document) == b"%PDF-CACHED"
    converter.assert_not_called()


def test_docx_preview_timeout_is_friendly(monkeypatch):
    document = _document()
    monkeypatch.setattr(previews, "file_exists", lambda _key: False)
    monkeypatch.setattr(previews, "download_file", lambda _key: b"docx")
    monkeypatch.setattr(
        previews,
        "_convert_docx",
        MagicMock(
            side_effect=previews.DocumentPreviewError(
                "preview_timeout", "Document preview took too long."
            )
        ),
    )

    with pytest.raises(previews.DocumentPreviewError) as error:
        previews.get_docx_preview(document)
    assert error.value.code == "preview_timeout"
    assert "too long" in error.value.message


def test_preview_rejects_unsupported_extension():
    with pytest.raises(previews.DocumentPreviewError) as error:
        previews.get_docx_preview(_document("invoice.docm"))
    assert error.value.code == "preview_unsupported"


def test_preview_uses_document_scope_for_unauthorized_document(monkeypatch):
    document_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}
    try:
        response = client.get(f"/api/documents/{document_id}/preview")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 404
