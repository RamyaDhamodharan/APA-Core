import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage
from src.services import email_invoice_backfill_services as backfill


def test_backfill_creates_review_invoice_runs_matching_and_audits(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="invoice",
        extracted_json={"document_type": "invoice", "total_amount": 42},
        status="extracted",
        failure_message="old failure",
    )
    invoice = SimpleNamespace(id=uuid.uuid4(), status="processing")
    document_query = MagicMock()
    document_results = (
        document_query.filter.return_value.order_by.return_value.with_for_update
    )
    document_results.return_value.all.return_value = [document]
    email_query = MagicMock()
    email_query.filter.return_value.order_by.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = lambda model: {
        Document: document_query,
        EmailMessage: email_query,
    }[model]

    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._save_invoice_from_extraction",
        lambda *_args: invoice,
    )
    monkeypatch.setattr(
        "src.api.rest.routers.document_routers._record_invoice_extraction_issues",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "src.services.llm_pipeline_services.validate_extracted_data",
        lambda *_args: ["Invoice number missing"],
    )
    matched = []
    monkeypatch.setattr(
        "src.services.invoice_matching_services.run_match_for_invoice",
        lambda _db, invoice_id, actor_id: matched.append((invoice_id, actor_id)),
    )
    monkeypatch.setattr(
        "src.services.document_workflow_services.refresh_related_workflows",
        lambda *_args: None,
    )

    actor = uuid.uuid4()
    summary = backfill.backfill_extracted_invoices(db, actor)

    assert summary == {
        "scanned": 1,
        "created": 1,
        "duplicates": 0,
        "failed": 0,
        "email_retries_scheduled": 0,
        "email_retry_ids": [],
    }
    assert matched == [(invoice.id, actor)]
    assert invoice.status == "needs_review"
    assert document.status == "needs_data_review"
    assert document.failure_message == "Invoice number missing"
    assert any(call.args[0].__class__ is AuditLog for call in db.add.call_args_list)

