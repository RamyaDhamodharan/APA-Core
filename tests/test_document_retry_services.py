import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models.audit_models import AuditLog
from src.services import document_retry_services as retries


def _query(document=None, candidates=None):
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.with_for_update.return_value = query
    query.limit.return_value = query
    query.first.return_value = document
    query.all.return_value = list(candidates or [])
    query.count.return_value = 0
    return query


def test_retryable_document_retries_three_times_then_fails(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(), status="received", retry_count=0, failure_class=None
    )
    db = MagicMock()
    db.query.return_value = _query(document)
    monkeypatch.setattr(retries, "SessionLocal", lambda: db)

    attempts = []

    def always_fails(_document_id):
        attempts.append(1)
        document.status = "failed"
        document.failure_class = "retryable"
        document.failure_message = "The processing service is unavailable."

    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._process_in_background",
        always_fails,
    )
    delays = []
    retries.process_document_with_retries(document.id, sleep=delays.append)

    assert len(attempts) == 4
    assert delays == [30, 120, 600]
    assert document.retry_count == 3
    assert document.failure_class == "retry_exhausted"
    assert "three automatic retries" in document.failure_message
    assert document.next_retry_at is None
    assert any(
        isinstance(call.args[0], AuditLog)
        and call.args[0].action == "document_processing_attempt"
        for call in db.add.call_args_list
    )


def test_permanent_failure_is_not_retried(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(), status="received", retry_count=0, failure_class=None
    )
    db = MagicMock()
    db.query.return_value = _query(document)
    monkeypatch.setattr(retries, "SessionLocal", lambda: db)
    attempts = []

    def permanent_failure(_document_id):
        attempts.append(1)
        document.status = "failed"
        document.failure_class = "permanent"
        document.failure_message = "This file appears damaged."

    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._process_in_background",
        permanent_failure,
    )
    delays = []
    retries.process_document_with_retries(document.id, sleep=delays.append)

    assert attempts == [1]
    assert delays == []
    assert document.retry_count == 0


def test_unexpected_worker_failure_moves_document_to_failed(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(),
        status="received",
        retry_count=0,
        failure_class=None,
        failure_message=None,
        next_retry_at=None,
    )
    db = MagicMock()
    db.query.return_value = _query(document)
    monkeypatch.setattr(retries, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._process_in_background",
        MagicMock(side_effect=RuntimeError("worker crash")),
    )

    assert retries.process_document_attempt(document.id) is None
    assert document.status == "failed"
    assert "stopped unexpectedly" in document.failure_message


def test_review_documents_are_not_automatically_retried(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(), status="needs_data_review", retry_count=0
    )
    db = MagicMock()
    db.query.return_value = _query(document)
    monkeypatch.setattr(retries, "SessionLocal", lambda: db)
    processed = []
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._process_in_background",
        lambda document_id: processed.append(document_id),
    )

    assert retries.process_document_attempt(document.id) is None
    assert processed == []
    assert document.status == "needs_data_review"


def test_duplicate_task_does_not_process_a_completed_document(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(), status="extracted", retry_count=0, failure_class=None
    )
    db = MagicMock()
    db.query.return_value = _query(document)
    monkeypatch.setattr(retries, "SessionLocal", lambda: db)
    processed = []
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._process_in_background",
        lambda document_id: processed.append(document_id),
    )

    assert retries.process_document_attempt(document.id) is None
    assert retries.process_document_attempt(document.id) is None
    assert processed == []


def test_admin_bulk_reprocess_is_idempotent_and_limited_to_five(monkeypatch):
    documents = [
        SimpleNamespace(
            id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            status="needs_data_review",
            failure_class="review",
            bulk_reprocessed_at=None,
            retry_count=0,
            next_retry_at=None,
            failure_message="Review this document.",
        )
        for _ in range(7)
    ]
    db = MagicMock()
    db.query.side_effect = [
        _query(candidates=documents[:5]),
        _query(),
        _query(candidates=documents[5:]),
        _query(),
    ]
    monkeypatch.setattr(retries, "_document_failed", lambda _id: False)
    processed = []

    first = retries.reprocess_documents_batch(
        db, uuid.uuid4(), processor=processed.append
    )
    second = retries.reprocess_documents_batch(
        db, uuid.uuid4(), processor=processed.append
    )

    assert first["scanned"] == first["batch_size"] == 5
    assert second["scanned"] == 2
    assert len(processed) == 7
    assert all(document.bulk_reprocessed_at is not None for document in documents)
    assert all(document.status == "received" for document in documents)


def test_failure_classification_and_retry_schedule():
    assert (
        retries.classify_document_failure(TimeoutError("provider timeout"))
        == "retryable"
    )
    assert (
        retries.classify_document_failure(ConnectionError("Redis connection failed"))
        == "retryable"
    )
    assert retries.classify_document_failure(ValueError("corrupt PDF")) == "permanent"
    assert (
        retries.classify_document_failure(ValueError("password-protected PDF"))
        == "permanent"
    )
    assert [retries.retry_delay_seconds(index) for index in range(3)] == [30, 120, 600]
