"""Idempotent backfill for extracted invoices and exhausted email retries."""

import uuid

from sqlalchemy import exists
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage
from src.models.invoice_models import Invoice


def backfill_extracted_invoices(db: Session, actor_id: uuid.UUID | None = None) -> dict:
    from src.api.rest.routers.document_routers import (
        DuplicateInvoiceError,
        _record_invoice_extraction_issues,
        _save_invoice_from_extraction,
    )
    from src.services.document_workflow_services import refresh_related_workflows
    from src.services.invoice_matching_services import run_match_for_invoice
    from src.services.llm_pipeline_services import validate_extracted_data

    summary = {"scanned": 0, "created": 0, "duplicates": 0, "failed": 0}
    documents = (
        db.query(Document)
        .filter(
            Document.doc_type == "invoice",
            Document.extracted_json.is_not(None),
            ~exists().where(Invoice.source_document_id == Document.id),
        )
        .order_by(Document.created_at.asc(), Document.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    for document in documents:
        summary["scanned"] += 1
        document_id = document.id
        data = document.extracted_json
        if not isinstance(data, dict):
            summary["failed"] += 1
            continue
        try:
            invoice = _save_invoice_from_extraction(db, document, data)
            issues = validate_extracted_data(data, "invoice")
            _record_invoice_extraction_issues(db, invoice, issues)
            run_match_for_invoice(db, invoice.id, actor_id)
            if issues:
                invoice.status = "needs_review"
                document.status = "needs_data_review"
            else:
                document.status = "extracted"
            document.failure_message = None if not issues else "; ".join(issues)[:500]
            db.flush()
            refresh_related_workflows(db, document.id)
            summary["created"] += 1
        except DuplicateInvoiceError as exc:
            document.status = "duplicate_blocked"
            if exc.original_invoice_id:
                document.extracted_json = {
                    **data,
                    "duplicate_of_invoice_id": str(exc.original_invoice_id),
                }
            document.failure_message = str(exc)[:500]
            summary["duplicates"] += 1
        except Exception:
            db.rollback()
            document = db.query(Document).filter_by(id=document_id).first()
            if document is not None:
                document.failure_message = (
                    "Invoice creation could not be completed. Retry processing "
                    "or contact support."
                )
            summary["failed"] += 1
        db.commit()

    failed_emails = (
        db.query(EmailMessage)
        .filter(EmailMessage.status == "failed_final")
        .order_by(EmailMessage.created_at.asc())
        .all()
    )
    retry_ids = []
    for message in failed_emails:
        message.status = "failed"
        message.attempts = 0
        message.next_retry_at = None
        message.failure_reason = None
        retry_ids.append(str(message.id))
    summary["email_retries_scheduled"] = len(retry_ids)
    summary["email_retry_ids"] = retry_ids
    db.add(
        AuditLog(
            action="email_invoice_backfill",
            entity_type="system",
            result="error" if summary["failed"] else "success",
            message="Extracted invoice backfill and exhausted email retry scheduled",
            output_summary={
                key: value
                for key, value in summary.items()
                if key != "email_retry_ids"
            },
            created_by=actor_id,
        )
    )
    db.commit()
    return summary
