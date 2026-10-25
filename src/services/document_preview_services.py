"""Generate and cache safe, derived previews for supported documents."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from src.models.document_models import Document
from src.storage.minio_storage import download_file, file_exists, upload_file

logger = logging.getLogger(__name__)
DOCX_PREVIEW_TIMEOUT_SECONDS = 30
DOCX_PREVIEW_MAX_BYTES = 20 * 1024 * 1024
_locks: dict[uuid.UUID, threading.Lock] = {}
_locks_guard = threading.Lock()


class DocumentPreviewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _lock_for(document_id: uuid.UUID) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(document_id, threading.Lock())


def _source_hash(document: Document) -> str:
    return str(document.sha256 or document.file_hash or "unknown")


def preview_object_key(document: Document) -> str:
    return f"derived/previews/{document.id}/{_source_hash(document)}.pdf"


def _max_bytes() -> int:
    try:
        return max(
            1, int(os.getenv("DOCX_PREVIEW_MAX_BYTES", str(DOCX_PREVIEW_MAX_BYTES)))
        )
    except ValueError:
        return DOCX_PREVIEW_MAX_BYTES


def _convert_docx(content: bytes, document_id: uuid.UUID) -> bytes:
    if len(content) > _max_bytes():
        raise DocumentPreviewError(
            "preview_size_limit", "This document is too large to preview."
        )
    with tempfile.TemporaryDirectory(prefix=f"docx-preview-{document_id}-") as temp:
        root = Path(temp)
        source = root / "source.docx"
        output = root / "output"
        profile = root / "profile"
        output.mkdir()
        source.write_bytes(content)
        command = [
            "soffice",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output),
            str(source),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=DOCX_PREVIEW_TIMEOUT_SECONDS,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "SAL_DISABLE_OPENCL": "1",
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                    "ALL_PROXY": "",
                    "NO_PROXY": "*",
                    "http_proxy": "",
                    "https_proxy": "",
                    "all_proxy": "",
                    "no_proxy": "*",
                },
            )
        except FileNotFoundError as exc:
            raise DocumentPreviewError(
                "preview_unavailable", "Document preview is temporarily unavailable."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DocumentPreviewError(
                "preview_timeout", "Document preview took too long."
            ) from exc
        if result.returncode != 0:
            raise DocumentPreviewError(
                "preview_conversion_failed", "This document could not be previewed."
            )
        pdf_path = output / "source.pdf"
        if not pdf_path.is_file():
            raise DocumentPreviewError(
                "preview_conversion_failed", "This document could not be previewed."
            )
        pdf = pdf_path.read_bytes()
        if not pdf.startswith(b"%PDF") or len(pdf) > _max_bytes():
            raise DocumentPreviewError(
                "preview_conversion_failed", "This document could not be previewed."
            )
        return pdf


def get_docx_preview(document: Document) -> bytes:
    if Path(document.original_filename).suffix.casefold() != ".docx":
        raise DocumentPreviewError(
            "preview_unsupported", "This file type does not support a preview."
        )
    key = preview_object_key(document)
    lock = _lock_for(document.id)
    with lock:
        try:
            if file_exists(key):
                cached = download_file(key)
                if cached.startswith(b"%PDF") and len(cached) <= _max_bytes():
                    return cached
        except Exception as exc:
            logger.debug("Preview cache lookup failed (%s)", type(exc).__name__)
        try:
            content = download_file(document.storage_path or "")
            pdf = _convert_docx(content, document.id)
            upload_file(key, pdf, "application/pdf")
            return pdf
        except DocumentPreviewError:
            raise
        except Exception as exc:
            logger.warning("Document preview storage failed (%s)", type(exc).__name__)
            raise DocumentPreviewError(
                "preview_unavailable", "Document preview is temporarily unavailable."
            ) from exc
