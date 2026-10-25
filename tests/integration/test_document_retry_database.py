import uuid

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.services import document_retry_services


def test_process_document_attempt_leaves_processing_document_failed(
    db_session, db_factories, monkeypatch
):
    user = db_factories["associate"]()
    document = Document(
        original_filename="worker-failure.pdf",
        file_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        status="received",
        source="upload",
        created_by=user.id,
    )
    db_session.add(document)
    db_session.flush()

    def fail_processing(document_id):
        row = db_session.get(Document, document_id)
        row.status = "failed"
        row.failure_class = "permanent"
        row.failure_message = "This file appears damaged."
        db_session.flush()

    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._process_in_background",
        fail_processing,
    )

    assert (
        document_retry_services.process_document_attempt(
            document.id, session_factory=lambda: db_session
        )
        is None
    )
    db_session.expire_all()
    saved = db_session.get(Document, document.id)
    assert saved.status == "failed"
    assert (
        db_session.query(AuditLog)
        .filter_by(entity_id=document.id, result="warning")
        .count()
        == 1
    )
    assert (
        db_session.query(AuditLog)
        .filter_by(entity_id=document.id, result="error")
        .count()
        == 1
    )
