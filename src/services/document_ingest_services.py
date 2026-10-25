"""
Application module for file ingestion services.
Flow: validate file extensions, enforce size limits, calculate SHA-256 hash, upload to MinIO, persist Document row, and dispatch asynchronous worker task.
Called by: document_routers, email_polling_services.
Calls: docx_validation_services, minio_storage, dispatch.
Tables: documents, audit_logs.
"""
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy.exc import IntegrityError

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.services.docx_validation_services import is_valid_docx
from src.storage.minio_storage import delete_file, upload_file
from src.worker.dispatch import dispatch_document_processing

logger = logging.getLogger(__name__)

# Permitted document extensions for ingestion
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml", ".docx"}
# Default maximum attachment upload limit (20 MB)
MAX_FILE_SIZE = 20 * 1024 * 1024


@dataclass
class IngestResult:
    """Standardized response container for document ingestion."""
    status: str
    document_id: uuid.UUID | None
    message: str


def ingest_file(
    db,
    filename: str,
    content_bytes: bytes,
    content_type: str | None,
    user_id: uuid.UUID | None,
    source: str,
    parent_document_id: uuid.UUID | None = None,
    email_meta: dict | None = None,
    background_tasks: BackgroundTasks | None = None,
    object_key: str | None = None,
    document_id: uuid.UUID | None = None,
    source_flag: str | None = None,
    internal_email_body: bool = False,
) -> IngestResult:
    """
    Validate, store, record, and dispatch asynchronous processing for an uploaded file or email attachment.
    Inputs: db (Session), filename (str), content_bytes (bytes), content_type (str | None), user_id (UUID | None), source (str), parent_document_id (UUID | None), email_meta (dict | None), background_tasks (BackgroundTasks | None), object_key (str | None), document_id (UUID | None), source_flag (str | None), internal_email_body (bool).
    Output: IngestResult with status ('received', 'failed', 'duplicate_blocked'), document_id, and user message.
    Effects: writes file to MinIO storage; creates rows in documents and audit_logs; queues Celery task.
    Results: entry point for end-to-end document OCR, classification, and extraction pipeline.
    """
    ingest_started = time.perf_counter()
    filename = Path(filename or "upload").name
    extension = Path(filename).suffix.lower()

    # Step 1: Validate file extension
    if extension not in ALLOWED_EXTENSIONS and not (
        internal_email_body and source == "email" and extension == ".txt"
    ):
        return IngestResult("failed", None, "Unsupported file type")

    # Step 2: Validate payload presence
    if not content_bytes:
        return IngestResult("failed", None, "Uploaded file is empty")

    # Step 3: Enforce file size limit
    max_size = (
        int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "20")) * 1024 * 1024
        if source in {"email", "email_poll"} and extension != ".eml"
        else MAX_FILE_SIZE
    )
    if len(content_bytes) > max_size:
        limit_label = f"{max_size // (1024 * 1024)} MB" if max_size % (1024 * 1024) == 0 else f"{max_size} byte"
        return IngestResult("failed", None, f"File exceeds {limit_label} limit")

    # Step 4: Validate DOCX structure integrity if applicable
    if extension == ".docx" and not is_valid_docx(
        filename, content_type, content_bytes
    ):
        return IngestResult(
            "failed", None, "Invalid Word document; upload a valid .docx file"
        )

    # Step 5: Check duplicate SHA-256 binary hash before uploading to MinIO
    file_hash = hashlib.sha256(content_bytes).hexdigest()
    existing = db.query(Document).filter_by(file_hash=file_hash).first()
    if existing:
        return IngestResult(
            "duplicate_blocked", existing.id, "This file has already been uploaded"
        )

    document_id = document_id or uuid.uuid4()
    document_meta = dict(email_meta or {})
    document_meta.setdefault("source_flag", source_flag)
    received_at = datetime.now(UTC)
    storage_path = object_key or (
        f"incoming/{received_at:%Y}/{received_at:%m}/{document_id}/{filename}"
    )

    # Step 6: Upload object to MinIO S3 bucket and persist Document database record
    try:
        upload_file(
            storage_path, content_bytes, content_type or "application/octet-stream"
        )
        document = Document(
            id=document_id,
            original_filename=filename,
            file_hash=file_hash,
            sha256=file_hash,
            mime=content_type,
            size=len(content_bytes),
            storage_path=storage_path,
            status="received",
            created_by=user_id,
            updated_by=user_id,
            parent_document_id=parent_document_id,
            source=source,
            **document_meta,
        )
        db.add(document)
        # Record formal audit log
        db.add(
            AuditLog(
                action="pipeline_step",
                entity_type="document",
                entity_id=document.id,
                step_name="receive",
                duration_ms=max(0, int((time.perf_counter() - ingest_started) * 1000)),
                input_ref=storage_path,
                output_summary={"filename": filename, "size_bytes": len(content_bytes)},
                result="success",
                message="Document received",
                created_by=user_id,
            )
        )
        db.commit()
        db.refresh(document)
    except IntegrityError:
        # Step 7: Handle race conditions on concurrent identical uploads
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception as cleanup_exc:
            if source in {"email", "email_poll"}:
                logger.warning(
                    "Email MinIO cleanup after duplicate failed (%s)",
                    type(cleanup_exc).__name__,
                )
            else:
                logger.error(
                    "MinIO cleanup failed after duplicate ingestion (%s)",
                    type(cleanup_exc).__name__,
                )
        existing = db.query(Document).filter_by(file_hash=file_hash).first()
        if existing is None and source in {"email", "email_poll"}:
            email_message_id = document_meta.get("email_message_id")
            if email_message_id:
                existing = (
                    db.query(Document)
                    .filter_by(
                        email_message_id=email_message_id,
                        parent_document_id=None,
                    )
                    .first()
                )
        return IngestResult(
            "duplicate_blocked",
            existing.id if existing else None,
            "This file has already been uploaded",
        )
    except Exception as exc:
        # Step 8: Clean up partial storage upload on error
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            if source in {"email", "email_poll"}:
                logger.warning("Email MinIO cleanup failed (%s)", type(exc).__name__)
            else:
                logger.error(
                    "MinIO cleanup failed after ingestion error (%s)",
                    type(exc).__name__,
                )
        if source in {"email", "email_poll"}:
            logger.warning("Email file ingestion failed (%s)", type(exc).__name__)
            raise RuntimeError(
                "Email file persistence failed while saving to storage/database: "
                f"{type(exc).__name__}: {exc}"
            ) from None
        logger.error("File ingestion failed for %s (%s)", filename, type(exc).__name__)
        return IngestResult("failed", None, str(exc)[:500] or "File ingestion failed")

    # Step 9: Dispatch background document extraction task (Celery or FastAPI background worker)
    if extension != ".eml":
        from src.api.rest.routers.document_routers import (
            _process_document_with_retries,
        )

        if background_tasks is not None:
            dispatch_document_processing(
                background_tasks, document.id, _process_document_with_retries
            )
        elif os.getenv("USE_CELERY", "false").lower() == "true":
            dispatch_document_processing(
                BackgroundTasks(), document.id, _process_document_with_retries
            )
        else:
            _process_document_with_retries(document.id)

    return IngestResult("received", document.id, "File received")
