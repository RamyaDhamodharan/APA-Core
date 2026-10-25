"""Generate deterministic, synthetic documents for the local backend E2E check."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any


@dataclass(frozen=True)
class SampleMail:
    subject: str
    message_id: str
    attachments: tuple[tuple[str, bytes, str], ...] = ()
    body: str = "Synthetic E2E mailbox message."


def pdf_bytes(payload: dict[str, Any]) -> bytes:
    """Build a small valid PDF whose text includes the deterministic stub JSON."""
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    chunks = [encoded[i : i + 84] for i in range(0, len(encoded), 84)]
    commands = ["BT /F1 8 Tf 30 800 Td (GW_AP_E2E_JSON_BEGIN) Tj"]
    for chunk in chunks:
        commands.append(f"0 -12 Td ({chunk.decode('ascii')}) Tj")
    commands.extend(["0 -12 Td (GW_AP_E2E_JSON_END) Tj", "ET"])
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode())
        result.extend(obj)
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(result)


def invoice_payload(
    *,
    vendor: str,
    invoice_number: str,
    po_number: str,
    unit_price: float = 20.0,
    quantity: int = 5,
    due_date: str = "2026-11-20",
    gl_code: str | None = None,
    field_confidence: dict[str, int] | None = None,
) -> dict[str, Any]:
    total = round(unit_price * quantity, 2)
    payload = {
        "document_type": "invoice",
        "vendor": vendor,
        "invoice_number": invoice_number,
        "order_number": po_number,
        "invoice_date": "2026-10-20",
        "due_date": due_date,
        "currency": "USD",
        "subtotal": total,
        "tax": 0,
        "total": total,
        "line_items": [
            {
                "item_code": "E2E-WIDGET",
                "description": "Synthetic E2E widget",
                "uom": "EA",
                "qty": quantity,
                "unit_price": unit_price,
                "line_amount": total,
            }
        ],
    }
    if gl_code is not None:
        payload["gl_code"] = gl_code
    if field_confidence is not None:
        payload["field_confidence"] = field_confidence
    return payload


def po_payload(
    *, vendor: str, po_number: str, unit_price: float = 20.0, quantity: int = 5
):
    total = round(unit_price * quantity, 2)
    return {
        "document_type": "po",
        "vendor": vendor,
        "order_number": po_number,
        "po_date": "2026-10-20",
        "currency": "USD",
        "subtotal": total,
        "tax": 0,
        "total": total,
        "line_items": [
            {
                "item_code": "E2E-WIDGET",
                "description": "Synthetic E2E widget",
                "uom": "EA",
                "qty": quantity,
                "unit_price": unit_price,
                "line_amount": total,
            }
        ],
    }


def build_mail(
    *,
    subject: str,
    message_id: str,
    attachments: tuple[tuple[str, bytes, str], ...] = (),
    body: str = "Synthetic E2E mailbox message.",
    sender: str = "supplier@localhost",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "ap-test@localhost"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    for name, value in (extra_headers or {}).items():
        message[name] = value
    message.set_content(body)
    for name, content, content_type in attachments:
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=name,
        )
    return message.as_bytes()
