"""
Unit tests for multi-signal confidence scoring, weights, hard caps, and gating rules.
Includes specific validation for Example Case 6 (blurry photo -> verification -> audit)
and Example Case 9 (corrupted PDF in bulk upload).
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid
from fastapi.testclient import TestClient
import pytest

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import invoice_routers, document_routers
from src.config.confidence_config import CONFIDENCE_CONFIG
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.services.confidence_scoring_services import (
    calculate_field_confidence,
    score_invoice_extraction,
)
from src.services.document_ingest_services import IngestResult

client = TestClient(app)


def test_confidence_config_defaults():
    cfg = CONFIDENCE_CONFIG
    assert cfg.threshold == 85.0
    assert round(cfg.weight_ocr + cfg.weight_consistency + cfg.weight_math + cfg.weight_master_data + cfg.weight_format, 2) == 1.00
    assert cfg.cap_format_invalid == 40.0
    assert cfg.cap_math_fail == 60.0
    assert cfg.cap_vendor_missing == 50.0


def test_perfect_invoice_scores_above_threshold():
    primary = {
        "vendor": "Acme Industrial Supplies",
        "invoice_number": "INV-2026-001",
        "invoice_date": "2026-10-20",
        "due_date": "2026-11-20",
        "gl_code": "6100",
        "subtotal": "100.00",
        "tax": "10.00",
        "total": "110.00",
        "line_items": [
            {
                "description": "High Grade Widgets",
                "qty": 5,
                "unit_price": 20.00,
                "line_amount": 100.00,
            }
        ],
    }
    breakdown = score_invoice_extraction(None, primary, primary, ocr_quality=98.0)
    assert not breakdown.requires_verification
    assert breakdown.overall_confidence >= 85
    assert breakdown.fields["amount"].confidence >= 85
    assert breakdown.fields["vendor"].confidence >= 85


def test_math_mismatch_caps_at_60_and_requires_verification():
    # Subtotal (100) + Tax (10) != Total (125) -> Mismatch!
    primary = {
        "vendor": "Acme Industrial Supplies",
        "invoice_number": "INV-2026-001",
        "invoice_date": "2026-10-20",
        "due_date": "2026-11-20",
        "gl_code": "6100",
        "subtotal": "100.00",
        "tax": "10.00",
        "total": "125.00",
        "line_items": [
            {
                "description": "Widgets",
                "qty": 5,
                "unit_price": 20.00,
                "line_amount": 100.00,
            }
        ],
    }
    breakdown = score_invoice_extraction(None, primary, primary, ocr_quality=100.0)
    assert breakdown.requires_verification
    assert breakdown.fields["amount"].confidence <= 60
    assert any("does not equal subtotal" in r for r in breakdown.fields["amount"].source_signals.reasons)


def test_vendor_missing_caps_at_50():
    primary = {
        "vendor": "",  # Missing vendor
        "invoice_number": "INV-2026-002",
        "total": "100.00",
        "due_date": "2026-11-20",
        "gl_code": "6100",
    }
    breakdown = score_invoice_extraction(None, primary, primary)
    assert breakdown.requires_verification
    assert breakdown.fields["vendor"].confidence <= 50


def test_format_invalid_caps_at_40():
    primary = {
        "vendor": "Acme Corp",
        "invoice_number": "INV-003",
        "total": "INVALID_AMOUNT",
        "due_date": "NOT_A_DATE",
        "gl_code": "6100",
    }
    breakdown = score_invoice_extraction(None, primary, primary)
    assert breakdown.requires_verification
    assert breakdown.fields["amount"].confidence <= 40
    assert breakdown.fields["due_date"].confidence <= 40


def test_blurry_image_penalty_lowers_confidence():
    primary = {
        "vendor": "Acme Corp",
        "invoice_number": "INV-004",
        "total": "100.00",
        "due_date": "2026-11-20",
        "gl_code": "6100",
    }
    sharp = score_invoice_extraction(None, primary, primary, ocr_quality=95.0, is_blurry=False)
    blurry = score_invoice_extraction(None, primary, primary, ocr_quality=95.0, is_blurry=True)
    assert blurry.overall_confidence < sharp.overall_confidence
    assert any("blurry" in r.lower() for r in blurry.fields["amount"].source_signals.reasons)


def test_example_case_6_blurry_photo_sent_to_verify_and_edit_audited(monkeypatch):
    """Case 6: Blurry photo yields low-confidence amount -> sent to associate, edit logged."""
    primary = {
        "vendor": "Acme Corp",
        "invoice_number": "INV-CASE-6",
        "total": "55.00",
        "due_date": "2026-11-20",
        "gl_code": "6000",
    }
    breakdown = score_invoice_extraction(None, primary, primary, ocr_quality=55.0, is_blurry=True)
    assert breakdown.requires_verification
    assert breakdown.fields["amount"].confidence < 85

    # Simulate invoice in 'verifying' state
    actor = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        workflow_status="verifying",
        match_status="pending",
        final_status=None,
        vendor_name_raw="Acme Corp",
        invoice_no="INV-CASE-6",
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=Decimal("50.00"),
        tax_amount=Decimal("5.00"),
        total_amount=Decimal("55.00"),
        po_number_raw="PO-600",
        po_id=None,
        vendor_id=None,
        gl_code="6000",
        field_confidence=breakdown.fields["amount"].to_dict(),
    )
    query, lines = MagicMock(), MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value.with_for_update.return_value.first.return_value = invoice
    lines.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [query, lines]

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(actor), "role": "associate"}
    monkeypatch.setattr(invoice_routers, "run_match_for_invoice", lambda *_a, **_k: None)

    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/verify",
            json={
                "values": {"total": "60.00"},
                "reason": "Corrected blurry total against document image",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert invoice.total_amount == Decimal("60.00")
    # Verify AuditLog was created with required audit metadata
    audit_calls = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog) and call.args[0].action == "invoice_field_edited"
    ]
    assert len(audit_calls) > 0
    logged = audit_calls[0]
    assert logged.new_value["reason"] == "Corrected blurry total against document image"
    assert logged.created_by == actor


def test_example_case_9_corrupt_pdf_in_bulk_upload_clear_error(monkeypatch):
    """Case 9: Corrupted PDF in bulk upload generates a clear error message while valid files continue."""
    results = iter(
        (
            IngestResult("received", uuid.uuid4(), "Accepted"),
            IngestResult("failed", None, "Bill could not be read: the PDF is corrupt or damaged. Ask the vendor for a new copy."),
        )
    )
    monkeypatch.setattr(document_routers, "ingest_file", lambda *_a, **_k: next(results))

    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4()), "role": "associate"}

    try:
        response = client.post(
            "/api/documents/upload",
            files=[
                ("file", ("valid_invoice.pdf", b"%PDF-1.4 valid", "application/pdf")),
                ("file", ("corrupted_invoice.pdf", b"corrupted data", "application/pdf")),
            ],
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    data = response.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["status"] == "received"
    assert data["results"][1]["status"] == "failed"
    assert "Bill could not be read" in data["results"][1]["message"]
