import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import document_routers
from src.db.session_db import get_db
from src.models.audit_models import AuditLog


def test_low_confidence_document_routes_to_other(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="contract.pdf",
        storage_path="documents/old.pdf",
        status="classified",
        created_by=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    db = MagicMock()
    moved = []
    monkeypatch.setattr(document_routers, "move_file", lambda *args: moved.append(args))
    document_routers._route_document_to_other(db, document, "invoice", 0.2)
    assert document.status == "other"
    assert document.storage_path == f"other/2026/10/{document.id}/contract.pdf"
    assert moved == [("documents/old.pdf", document.storage_path)]
    assert isinstance(db.add.call_args.args[0], AuditLog)
    db.commit.assert_called_once()


def test_reclassify_other_moves_object_and_audits_user(monkeypatch):
    user_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="misc.pdf",
        storage_path="documents/other/original/misc.pdf",
        status="other",
        doc_type="other",
        updated_by=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    db = MagicMock()
    locked_query = (
        db.query.return_value.filter.return_value.with_for_update.return_value
    )
    locked_query.first.return_value = document
    moved = []
    dispatched = []
    monkeypatch.setattr(document_routers, "move_file", lambda *args: moved.append(args))
    monkeypatch.setattr(
        document_routers,
        "dispatch_document_processing",
        lambda *args: dispatched.append(args),
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id)}
    try:
        response = TestClient(app).post(
            f"/api/documents/{document.id}/reclassify", json={"doc_type": "invoice"}
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert document.status == "received"
    assert document.classify_override == "invoice"
    assert document.updated_by == user_id
    assert moved == [
        (
            "documents/other/original/misc.pdf",
            f"invoice/2026/10/{document.id}/misc.pdf",
        )
    ]
    assert dispatched
    audit = db.add.call_args.args[0]
    assert isinstance(audit, AuditLog)
    assert audit.created_by == user_id


def test_cheap_classifier_uses_filename_and_page_text_without_paid_call():
    assert document_routers._classify_document_cheaply("supplier_invoice.pdf", "") == (
        "invoice",
        0.95,
    )
    assert document_routers._classify_document_cheaply(
        "attachment.pdf", "PURCHASE ORDER\nPO Number: 123"
    ) == ("po", 0.95)
    assert (
        document_routers._classify_document_cheaply(
            "random.pdf", "terms and conditions"
        )
        is None
    )
