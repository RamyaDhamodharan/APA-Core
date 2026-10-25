import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rest.routers.command_center_routers import command_center_bills
from src.api.rest.routers.document_routers import _save_invoice_from_extraction
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_draft_models import EmailDraft
from src.models.exchange_rate_models import ExchangeRate
from src.models.follow_up_models import FollowUp
from src.models.invoice_match_models import InvoiceDiscrepancy
from src.models.invoice_models import Invoice
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.models.notification_models import Notification
from src.models.purchase_order_models import PurchaseOrderLine
from src.services import follow_up_services, invoice_matching_services
from src.services.invoice_number_services import find_duplicate_invoice


def _high_confidence():
    fields = (
        "vendor",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax",
        "total",
        "order_number",
        "gl_code",
    )
    return {field: {"confidence": 95, "derived": False} for field in fields}


def test_case_1_matching_invoice_po_and_receipt_auto_moves_to_ready_to_pay(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "false")
    user = db_factories["associate"]()
    vendor = db_factories["vendor"]()
    po = db_factories["purchase_order"](
        vendor,
        created_by=user.id,
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("10.00"),
        total_amount=Decimal("110.00"),
        lines=[
            {
                "item_code": "ITEM-1",
                "description": "Widget",
                "uom": "EA",
                "qty": Decimal("10"),
                "unit_price": Decimal("10.00"),
                "line_amount": Decimal("100.00"),
                "created_by": user.id,
            }
        ],
    )
    po_line = db_session.query(PurchaseOrderLine).filter_by(po_id=po.id).one()
    db_factories["goods_receipt"](
        po,
        created_by=user.id,
        lines=[
            {
                "po_line_id": po_line.id,
                "item_code": "ITEM-1",
                "description": "Widget",
                "uom": "EA",
                "received_qty": Decimal("10"),
                "accepted_qty": Decimal("10"),
                "rejected_qty": Decimal("0"),
                "created_by": user.id,
            }
        ],
    )
    invoice = db_factories["invoice"](
        vendor,
        invoice_no="INV-READY-1",
        po_id=po.id,
        po_number_raw=po.po_number,
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("10.00"),
        total_amount=Decimal("110.00"),
        workflow_status="matching",
        field_confidence=_high_confidence(),
        created_by=user.id,
        lines=[
            {
                "po_line_id": po_line.id,
                "item_code": "ITEM-1",
                "description": "Widget",
                "uom": "EA",
                "qty": Decimal("10"),
                "unit_price": Decimal("10.00"),
                "line_amount": Decimal("100.00"),
                "created_by": user.id,
            }
        ],
    )

    run = invoice_matching_services.run_match_for_invoice(
        db_session, invoice.id, user.id
    )

    assert run.result == "match_ok"
    assert invoice.status == "ready_to_pay"


def test_case_5_unknown_vendor_rejected_with_unsent_sender_draft(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "true")
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "")
    document = Document(
        original_filename="unknown-vendor.eml",
        file_hash=uuid.uuid4().hex + "a" * 32,
        status="extracted",
        doc_type="invoice",
        source="email",
        email_sender="sender@example.test",
        email_subject="Invoice",
    )
    db_session.add(document)
    db_session.flush()
    invoice = db_factories["invoice"](
        invoice_no="UNKNOWN-VENDOR-1",
        source_document_id=document.id,
        vendor_name_raw="Unlisted Vendor",
        workflow_status="matching",
        field_confidence=_high_confidence(),
    )

    run = invoice_matching_services.run_match_for_invoice(db_session, invoice.id)

    draft = db_session.query(EmailDraft).filter_by(invoice_id=invoice.id).one()
    assert run.result == "no_match"
    assert invoice.workflow_status == "rejected"
    assert invoice.final_status == "rejected"
    assert draft.to_addresses == ["sender@example.test"]
    assert draft.status == "draft"
    assert draft.blocked_by_allowlist is True
    queued_notifications = db_session.query(Notification).filter_by(
        document_id=document.id
    )
    assert queued_notifications.count() == 0


def test_case_7_mark_overdue_creates_history_alert_and_follow_up(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "")
    invoice = db_factories["invoice"](
        invoice_no="OVERDUE-1",
        status="ready_to_pay",
        workflow_status="approved",
        final_status="approved",
        due_date=date.today() - timedelta(days=5),
        total_amount=Decimal("75.00"),
    )

    changed = follow_up_services.mark_overdue(db_session, datetime.now(UTC))
    follow_up_services.run_follow_ups(db_session, datetime.now(UTC))
    command_center = command_center_bills(
        bucket="overdue", db=db_session, _user={}
    )

    assert changed == 1
    assert invoice.workflow_status == "overdue"
    assert next(
        row for row in command_center["items"] if row["invoice_id"] == invoice.id
    )["alert"] is True
    assert db_session.query(InvoiceStateHistory).filter_by(
        invoice_id=invoice.id, to_status="overdue"
    ).count() == 1
    assert db_session.query(AuditLog).filter_by(
        entity_type="invoice", entity_id=invoice.id, action="invoice_overdue"
    ).count() == 1
    notifications = db_session.query(Notification).filter_by(
        document_id=invoice.source_document_id
    )
    assert notifications.count() == 0
    follow_up = db_session.query(FollowUp).filter_by(
        entity_type="invoice", entity_id=invoice.id, kind="overdue"
    ).one()
    assert follow_up.attempts >= 1


def _run_currency_mismatch_pipeline(db_session, db_factories, *, with_exchange_rate):
    user = db_factories["associate"]()
    vendor = db_factories["vendor"]()
    po = db_factories["purchase_order"](
        vendor,
        po_number=f"PO-CURRENCY-{uuid.uuid4()}",
        currency="USD",
        created_by=user.id,
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("100.00"),
        lines=[
            {
                "item_code": "CURRENCY-ITEM",
                "description": "Currency test item",
                "uom": "EA",
                "qty": Decimal("10"),
                "unit_price": Decimal("10.00"),
                "line_amount": Decimal("100.00"),
                "created_by": user.id,
            }
        ],
    )
    po_line = db_session.query(PurchaseOrderLine).filter_by(po_id=po.id).one()
    db_factories["goods_receipt"](
        po,
        created_by=user.id,
        lines=[
            {
                "po_line_id": po_line.id,
                "item_code": po_line.item_code,
                "description": po_line.description,
                "uom": po_line.uom,
                "received_qty": Decimal("10"),
                "accepted_qty": Decimal("10"),
                "rejected_qty": Decimal("0"),
                "created_by": user.id,
            }
        ],
    )
    document = Document(
        original_filename="currency-mismatch.pdf",
        file_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        status="extracted",
        doc_type="invoice",
        source="upload",
        created_by=user.id,
    )
    db_session.add(document)
    db_session.flush()
    if with_exchange_rate:
        db_session.add(
            ExchangeRate(
                from_currency="EUR",
                to_currency="USD",
                rate=Decimal("1.100000"),
                rate_date=date.today(),
                created_by=user.id,
            )
        )
        db_session.flush()

    extracted = {
        "vendor": vendor.name,
        "invoice_number": f"INV-CURRENCY-{uuid.uuid4()}",
        "order_number": po.po_number,
        "invoice_date": "2026-10-01",
        "currency": "EUR",
        "subtotal": "100.00",
        "tax": "0.00",
        "total": "100.00",
        "line_items": [
            {
                "item_code": po_line.item_code,
                "description": po_line.description,
                "uom": po_line.uom,
                "qty": "10",
                "unit_price": "10.00",
                "line_amount": "100.00",
            }
        ],
        "field_confidence": _high_confidence(),
    }
    invoice = _save_invoice_from_extraction(db_session, document, extracted)
    db_session.flush()

    run = invoice_matching_services.run_match_for_invoice(
        db_session, invoice.id, user.id
    )
    db_session.flush()
    discrepancy = (
        db_session.query(InvoiceDiscrepancy)
        .filter_by(invoice_id=invoice.id, field="currency")
        .one()
    )
    return document, invoice, run, discrepancy


def test_currency_mismatch_persists_invoice_and_keeps_document_extracted(
    db_session, db_factories
):
    document, invoice, run, discrepancy = _run_currency_mismatch_pipeline(
        db_session, db_factories, with_exchange_rate=False
    )

    assert db_session.get(Invoice, invoice.id) is invoice
    assert run.result == "match_issues"
    assert invoice.status == "needs_review"
    assert discrepancy.reason == "Currency mismatch: invoice EUR, PO USD"
    assert discrepancy.invoice_value == "EUR"
    assert discrepancy.po_value == "USD"
    assert document.status == "extracted"
    assert document.failure_message is None


def test_currency_mismatch_with_exchange_rate_still_requires_review(
    db_session, db_factories
):
    document, invoice, run, discrepancy = _run_currency_mismatch_pipeline(
        db_session, db_factories, with_exchange_rate=True
    )

    assert db_session.get(Invoice, invoice.id) is invoice
    assert run.result == "match_issues"
    assert invoice.status == "needs_review"
    assert discrepancy.reason == "Currency mismatch: invoice EUR, PO USD"
    assert discrepancy.invoice_value == "EUR"
    assert discrepancy.po_value == "USD"
    assert document.status == "extracted"
    assert document.failure_message is None


def test_concurrent_canonical_invoice_number_insert_has_one_duplicate_winner(
    test_engine,
):
    from src.models.vendor_models import Vendor

    with Session(test_engine) as seed_session:
        vendor = Vendor(name=f"Race Vendor {uuid.uuid4()}")
        seed_session.add(vendor)
        seed_session.commit()
        vendor_id = vendor.id

    def insert(number):
        with Session(test_engine) as session:
            session.add(
                Invoice(
                    vendor_id=vendor_id,
                    invoice_no=number,
                    status="processing",
                    workflow_status="received",
                    match_status="pending",
                    field_confidence={},
                )
            )
            try:
                session.commit()
                return "created"
            except IntegrityError:
                session.rollback()
                duplicate = find_duplicate_invoice(
                    session,
                    vendor_id,
                    "INV001",
                )
                return "duplicate" if duplicate is not None else "unhandled conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(insert, ["INV-001", "inv 001"]))
        assert sorted(outcomes) == ["created", "duplicate"]
    finally:
        with Session(test_engine) as cleanup:
            cleanup.query(Invoice).filter_by(vendor_id=vendor_id).delete()
            cleanup.query(Vendor).filter_by(id=vendor_id).delete()
            cleanup.commit()
