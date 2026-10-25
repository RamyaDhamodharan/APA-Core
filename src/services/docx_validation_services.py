"""Validation helpers for macro-free Office Open XML Word documents."""

from __future__ import annotations

import zlib
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, LargeZipFile, ZipFile

DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
DOCX_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
DOCX_MAX_ZIP_ENTRIES = 1000
_DOCX_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)


def is_valid_docx(filename: str, content_type: str | None, content: bytes) -> bool:
    """Verify the extension, declared MIME type, and bounded OOXML ZIP structure."""
    if Path(filename).suffix.casefold() != ".docx":
        return False
    declared_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if declared_type != DOCX_MIME_TYPE:
        return False
    if not content:
        return False

    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > DOCX_MAX_ZIP_ENTRIES:
                return False
            if sum(entry.file_size for entry in entries) > DOCX_MAX_UNCOMPRESSED_BYTES:
                return False
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return False
            if any("vbaproject.bin" in name.casefold() for name in names):
                return False
            if any(entry.flag_bits & 0x1 for entry in entries):
                return False
            if archive.testzip() is not None:
                return False
            content_types_info = archive.getinfo("[Content_Types].xml")
            if content_types_info.file_size > 1024 * 1024:
                return False
            content_types_xml = archive.read("[Content_Types].xml")
            upper_content_types = content_types_xml.upper()
            if (
                b"<!DOCTYPE" in upper_content_types
                or b"<!ENTITY" in upper_content_types
            ):
                return False
            content_types = ElementTree.fromstring(content_types_xml)
            document_override = next(
                (
                    node
                    for node in content_types
                    if node.tag.rsplit("}", 1)[-1] == "Override"
                    and node.attrib.get("PartName") == "/word/document.xml"
                ),
                None,
            )
            return bool(
                document_override is not None
                and document_override.attrib.get("ContentType")
                == _DOCX_MAIN_CONTENT_TYPE
            )
    except (
        BadZipFile,
        LargeZipFile,
        OSError,
        RuntimeError,
        ElementTree.ParseError,
        KeyError,
        ValueError,
        EOFError,
        NotImplementedError,
        zlib.error,
    ):
        return False
