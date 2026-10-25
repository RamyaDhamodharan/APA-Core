import hashlib
import io
import logging
import os
import re
import unicodedata
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.invoice_models import Invoice
from src.models.invoice_supporting_document_models import InvoiceSupportingDocument
from src.models.user_models import User
from src.schemas.invoice_supporting_document_schemas import (
    SupportingDocumentItem,
)
from src.storage.minio_storage import delete_file, download_file, upload_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/invoices", tags=["Invoice supporting documents"])

_CATEGORY_VALUES = {
    "delivery_note",
    "contract",
    "approval_email",
    "tax_certificate",
    "other",
}
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}


def _actor_id(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _configured_limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _safe_filename(filename: str | None) -> str:
    base = Path((filename or "supporting-document").replace("\\", "/")).name
    normalized = unicodedata.normalize("NFKD", base)
    safe = re.sub(r"[^\w.-]+", "_", normalized, flags=re.ASCII).strip("._")
    return (safe or "supporting-document")[:200]


def _matches_content(filename: str, content_type: str | None, content: bytes) -> bool:
    extension = Path(filename).suffix.lower()
    expected = _CONTENT_TYPES.get(extension)
    supplied = (content_type or "").split(";", 1)[0].strip().lower()
    if expected is None or supplied != expected:
        return False
    if extension == ".pdf":
        return content.lstrip().startswith(b"%PDF-")
    if extension == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if extension == ".txt":
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False
        return b"\x00" not in content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    if extension == ".docx":
        return "[Content_Types].xml" in names and "word/document.xml" in names
    return "[Content_Types].xml" in names and "xl/workbook.xml" in names


async def _read_file_limited(file: UploadFile, limit: int) -> bytes | None:
    pieces: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = await file.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            return b"".join(pieces)
        pieces.append(chunk)
        total += len(chunk)
    return None


def _audit(
    db: Session,
    *,
    invoice_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    file_name: str,
    category: str,
    result: str,
    description: str,
) -> AuditLog:
    event = AuditLog(
        action=action,
        entity_type="invoice",
        entity_id=invoice_id,
        result=result,
        message=description[:500],
        new_value={
            "file_name": file_name,
            "category": category,
            "result": result,
        },
        created_by=user_id,
    )
    db.add(event)
    return event


def _audit_upload_error(
    db: Session,
    invoice_id: uuid.UUID,
    user_id: uuid.UUID,
    file_name: str,
    category: str,
    message: str,
    *,
    status: str = "rejected",
    result: str = "error",
) -> dict:
    _audit(
        db,
        invoice_id=invoice_id,
        user_id=user_id,
        action="supporting_document_upload",
        file_name=file_name,
        category=category,
        result=result,
        description=message,
    )
    db.commit()
    return {"file_name": file_name, "status": status, "message": message}


@router.post("/{invoice_id}/supporting-documents")
async def upload_invoice_supporting_documents(
    invoice_id: uuid.UUID,
    files: Annotated[list[UploadFile], File(...)],
    category: Annotated[str, Form(...)],
    description: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    user_id = _actor_id(current_user)
    max_files = _configured_limit("INVOICE_SUPPORTING_MAX_FILES", 10)
    max_bytes = _configured_limit("INVOICE_SUPPORTING_MAX_FILE_MB", 20) * 1024 * 1024
    results = []

    for index, file in enumerate(files):
        file_name = _safe_filename(file.filename)
        if index >= max_files:
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    f"Request limit is {max_files} files",
                )
            )
            continue
        if category not in _CATEGORY_VALUES:
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    "Invalid document category",
                )
            )
            continue
        try:
            content = await _read_file_limited(file, max_bytes)
        except Exception as exc:
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    f"File could not be read ({type(exc).__name__})",
                )
            )
            continue
        if content is None:
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    f"File exceeds the {max_bytes // (1024 * 1024)} MB limit",
                )
            )
            continue
        if not content:
            results.append(
                _audit_upload_error(
                    db, invoice_id, user_id, file_name, category, "File is empty"
                )
            )
            continue
        if not _matches_content(file_name, file.content_type, content):
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    "File extension, content type, or file signature is invalid",
                )
            )
            continue

        digest = hashlib.sha256(content).hexdigest()
        duplicate = (
            db.query(InvoiceSupportingDocument)
            .filter(
                InvoiceSupportingDocument.invoice_id == invoice_id,
                InvoiceSupportingDocument.sha256 == digest,
                InvoiceSupportingDocument.deleted_at.is_(None),
            )
            .first()
        )
        if duplicate is not None:
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    "An active supporting document with identical "
                    "content already exists",
                    status="duplicate_blocked",
                    result="warning",
                )
            )
            continue

        now = datetime.now(UTC)
        document_id = uuid.uuid4()
        storage_key = (
            f"supporting/{now:%Y}/{now:%m}/{invoice_id}/{document_id}_{file_name}"
        )
        canonical_type = _CONTENT_TYPES[Path(file_name).suffix.lower()]
        uploaded = False
        try:
            upload_file(storage_key, content, canonical_type)
            uploaded = True
            document = InvoiceSupportingDocument(
                id=document_id,
                invoice_id=invoice_id,
                file_name=file_name,
                content_type=canonical_type,
                size_bytes=len(content),
                sha256=digest,
                storage_key=storage_key,
                category=category,
                description=(description.strip() or None) if description else None,
                uploaded_by=user_id,
            )
            db.add(document)
            _audit(
                db,
                invoice_id=invoice_id,
                user_id=user_id,
                action="supporting_document_upload",
                file_name=file_name,
                category=category,
                result="success",
                description="Supporting document uploaded",
            )
            db.commit()
            results.append(
                {
                    "file_name": file_name,
                    "status": "uploaded",
                    "id": str(document_id),
                    "size_bytes": len(content),
                    "category": category,
                }
            )
        except IntegrityError:
            db.rollback()
            if uploaded:
                try:
                    delete_file(storage_key)
                except Exception:
                    logger.warning("Supporting document cleanup failed")
            duplicate = (
                db.query(InvoiceSupportingDocument)
                .filter(
                    InvoiceSupportingDocument.invoice_id == invoice_id,
                    InvoiceSupportingDocument.sha256 == digest,
                    InvoiceSupportingDocument.deleted_at.is_(None),
                )
                .first()
            )
            if duplicate is not None:
                results.append(
                    _audit_upload_error(
                        db,
                        invoice_id,
                        user_id,
                        file_name,
                        category,
                        "An active supporting document with identical "
                        "content already exists",
                        status="duplicate_blocked",
                        result="warning",
                    )
                )
            else:
                results.append(
                    _audit_upload_error(
                        db,
                        invoice_id,
                        user_id,
                        file_name,
                        category,
                        "Upload could not be saved",
                    )
                )
        except Exception as exc:
            db.rollback()
            if uploaded:
                try:
                    delete_file(storage_key)
                except Exception:
                    logger.warning("Supporting document cleanup failed")
            results.append(
                _audit_upload_error(
                    db,
                    invoice_id,
                    user_id,
                    file_name,
                    category,
                    f"Upload failed ({type(exc).__name__})",
                )
            )

    return {"results": results}


@router.get("/{invoice_id}/supporting-documents")
def list_invoice_supporting_documents(
    invoice_id: uuid.UUID,
    include_deleted: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    if db.query(Invoice).filter(Invoice.id == invoice_id).first() is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if include_deleted and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    query = (
        db.query(InvoiceSupportingDocument, User.name)
        .outerjoin(User, InvoiceSupportingDocument.uploaded_by == User.id)
        .filter(InvoiceSupportingDocument.invoice_id == invoice_id)
    )
    if not include_deleted:
        query = query.filter(InvoiceSupportingDocument.deleted_at.is_(None))
    rows = query.order_by(InvoiceSupportingDocument.uploaded_at.desc()).all()
    return [
        SupportingDocumentItem.model_validate(
            {**document.__dict__, "uploaded_by_name": name}
        ).model_dump(mode="json")
        for document, name in rows
    ]


@router.get("/{invoice_id}/supporting-documents/{document_id}/file")
def download_invoice_supporting_document(
    invoice_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if db.query(Invoice).filter(Invoice.id == invoice_id).first() is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    user_id = _actor_id(current_user)
    document = (
        db.query(InvoiceSupportingDocument)
        .filter(
            InvoiceSupportingDocument.id == document_id,
            InvoiceSupportingDocument.invoice_id == invoice_id,
            InvoiceSupportingDocument.deleted_at.is_(None),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Supporting document not found")
    try:
        content = download_file(document.storage_key)
    except Exception as exc:
        _audit(
            db,
            invoice_id=invoice_id,
            user_id=user_id,
            action="supporting_document_viewed",
            file_name=document.file_name,
            category=document.category,
            result="error",
            description="Supporting document download failed",
        )
        db.commit()
        logger.warning("Supporting document download failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="File could not be retrieved"
        ) from None
    _audit(
        db,
        invoice_id=invoice_id,
        user_id=user_id,
        action="supporting_document_viewed",
        file_name=document.file_name,
        category=document.category,
        result="success",
        description="Supporting document viewed",
    )
    db.commit()
    headers = {
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(document.file_name)}"
        )
    }
    return StreamingResponse(
        iter([content]), media_type=document.content_type, headers=headers
    )


@router.delete("/{invoice_id}/supporting-documents/{document_id}")
def delete_invoice_supporting_document(
    invoice_id: uuid.UUID,
    document_id: uuid.UUID,
    reason: Annotated[str, Body(embed=True)],
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    if db.query(Invoice).filter(Invoice.id == invoice_id).first() is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A deletion reason is required")
    user_id = _actor_id(current_user)
    document = (
        db.query(InvoiceSupportingDocument)
        .filter(
            InvoiceSupportingDocument.id == document_id,
            InvoiceSupportingDocument.invoice_id == invoice_id,
            InvoiceSupportingDocument.deleted_at.is_(None),
        )
        .with_for_update()
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Supporting document not found")
    document.deleted_at = datetime.now(UTC)
    document.deleted_by = user_id
    document.deleted_reason = reason[:2000]
    _audit(
        db,
        invoice_id=invoice_id,
        user_id=user_id,
        action="supporting_document_deleted",
        file_name=document.file_name,
        category=document.category,
        result="success",
        description=f"Supporting document deleted: {reason[:350]}",
    )
    db.commit()
    return {"id": str(document.id), "status": "deleted"}
