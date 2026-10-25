import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.document_workflow_services import (
    compare_available_records,
    evaluate_document_workflow,
    recommend_invoice_review,
)


class _Query:
    def __init__(self, model, rows):
        self.model = model
        self.rows = rows
        self.criteria = []

    def filter(self, *criteria):
        self.criteria.extend(criteria)
        return self

    def options(self, *_options):
        return self

    def join(self, *_entities):
        return self

    def all(self):
        return self._filtered()

    def first(self):
        rows = self._filtered()
        return rows[0] if rows else None

    def _filtered(self):
        rows = list(self.rows)
        for criterion in self.criteria:
            left = getattr(criterion, "left", None)
            key = getattr(left, "key", None)
            right = getattr(getattr(criterion, "right", None), "value", None)
            operator = getattr(getattr(criterion, "operator", None), "__name__", "")
            if key is None:
                continue
            if operator == "is_":
                right = True
                rows = [row for row in rows if getattr(row, key, None) is right]
            elif operator == "eq":
                rows = [row for row in rows if getattr(row, key, None) == right]
            elif operator == "ne":
                rows = [row for row in rows if getattr(row, key, None) != right]
        return rows


class _Session:
    def __init__(self, records):
        self.records = records

    def get(self, model, record_id):
        return next(
            (row for row in self.records.get(model, []) if row.id == record_id), None
        )

    def query(self, *models):
        model = models[0]
        return _Query(model, self.records.get(model, []))


def _workflow_session(
    *, with_po=True, with_receipt=True, with_invoice=True, duplicate_po=False
):
    owner_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    po_id = uuid.uuid4()
    po_line_id = uuid.uuid4()
    invoice_doc = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="invoice",
        status="extracted",
        extracted_json={"order_number": "PO-1"},
        created_by=owner_id,
    )
    po_doc = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="po",
        status="extracted",
        extracted_json={"po_number": "PO-1"},
        created_by=owner_id,
    )
    gr_doc = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="goods_receipt",
        status="extracted",
        extracted_json={
            "order_number": "PO-1",
            "vendor": "Vendor A",
            "line_items": [
                {
                    "item_code": "SKU-1",
                    "description": "Widget",
                    "uom": "EA",
                    "received_qty": "10",
                    "accepted_qty": "10",
                    "rejected_qty": "0",
                }
            ],
        },
        created_by=owner_id,
    )
    po = SimpleNamespace(
        id=po_id,
        source_document_id=po_doc.id,
        vendor_id=vendor_id,
        po_number="PO-1",
        currency="USD",
        total_amount=Decimal("100"),
        subtotal=Decimal("100"),
        tax_amount=Decimal("0"),
        is_current=True,
        po_date=None,
        created_by=owner_id,
    )
    po_line = SimpleNamespace(
        id=po_line_id,
        po_id=po_id,
        item_code="SKU-1",
        description="Widget",
        uom="EA",
        qty=Decimal("10"),
        unit_price=Decimal("10"),
        line_amount=Decimal("100"),
    )
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=invoice_doc.id,
        vendor_id=vendor_id,
        vendor_name_raw="Vendor A",
        invoice_no="INV-1",
        invoice_no_normalized="INV1",
        po_number_raw="PO-1",
        po_id=po_id if with_po else None,
        currency="USD",
        total_amount=Decimal("100"),
        subtotal=Decimal("100"),
        tax_amount=Decimal("0"),
        field_confidence={
            field: {"confidence": 95, "derived": False}
            for field in (
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
        },
        is_current=True,
        final_status=None,
        created_by=owner_id,
        owner_user_id=owner_id,
    )
    invoice_line = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        po_line_id=po_line_id,
        item_code="SKU-1",
        description="Widget",
        uom="EA",
        qty=Decimal("10"),
        unit_price=Decimal("10"),
        line_amount=Decimal("100"),
    )
    receipt_line = SimpleNamespace(
        po_line_id=po_line_id,
        uom="EA",
        received_qty=Decimal("10"),
        accepted_qty=Decimal("10"),
        rejected_qty=Decimal("0"),
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=gr_doc.id,
        po_id=po_id,
        vendor_id=vendor_id,
        gr_number="GR-1",
        lines=[receipt_line],
        created_by=owner_id,
    )
    purchase_orders = [po]
    if duplicate_po:
        purchase_orders.append(
            SimpleNamespace(
                **{**po.__dict__, "id": uuid.uuid4(), "source_document_id": None}
            )
        )
    documents = [invoice_doc] if with_invoice else []
    if with_po:
        documents.append(po_doc)
    if with_receipt:
        documents.append(gr_doc)
    rows = {
        Document: documents,
        PurchaseOrder: purchase_orders if with_po else [],
        PurchaseOrderLine: [po_line] if with_po else [],
        Invoice: [invoice] if with_invoice else [],
        InvoiceLine: [invoice_line] if with_invoice else [],
        GoodsReceipt: [receipt] if with_receipt and with_po else [],
    }
    sources = {document.id: document for document in documents}
    sources.update({po.id: po for po in purchase_orders})
    return _Session(rows), rows, sources, invoice_doc, po_doc, gr_doc, invoice, owner_id


def _records(*, invoice_qty="4", accepted_qty="4", invoice_price="10"):
    po_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    po_line_id = uuid.uuid4()
    po_line = SimpleNamespace(
        id=po_line_id,
        item_code="SKU-1",
        description="Widget",
        uom="EA",
        qty=Decimal("10"),
        unit_price=Decimal("10"),
    )
    invoice = SimpleNamespace(
        vendor_id=vendor_id,
        po_number_raw="PO-1",
        currency="USD",
        total_amount=Decimal(str(invoice_qty)) * Decimal(str(invoice_price)),
        subtotal=Decimal(str(invoice_qty)) * Decimal(str(invoice_price)),
        tax_amount=Decimal("0"),
    )
    invoice_line = SimpleNamespace(
        po_line_id=po_line_id,
        item_code="SKU-1",
        description="Widget",
        uom="EA",
        qty=Decimal(str(invoice_qty)),
        unit_price=Decimal(str(invoice_price)),
        line_amount=Decimal(str(invoice_qty)) * Decimal(str(invoice_price)),
    )
    receipt_line = SimpleNamespace(
        po_line_id=po_line_id,
        uom="EA",
        received_qty=Decimal(str(accepted_qty)),
        accepted_qty=Decimal(str(accepted_qty)),
        rejected_qty=Decimal("0"),
    )
    purchase_order = SimpleNamespace(
        id=po_id,
        vendor_id=vendor_id,
        po_number="PO-1",
        currency="USD",
        total_amount=Decimal("100"),
        subtotal=Decimal("100"),
        tax_amount=Decimal("0"),
    )
    receipt = SimpleNamespace(
        po_id=po_id,
        vendor_id=vendor_id,
        lines=[receipt_line],
    )
    return invoice, purchase_order, receipt, po_line, invoice_line


def _checks(**kwargs):
    invoice, purchase_order, receipt, po_line, invoice_line = _records(**kwargs)
    return compare_available_records(
        invoice=invoice,
        purchase_order=purchase_order,
        receipts=[receipt],
        po_lines=[po_line],
        invoice_lines=[invoice_line],
    )


def test_workflow_comparison_accepts_an_invoice_for_a_partial_delivery():
    checks = _checks()

    assert checks
    assert all(check.outcome == "passed" for check in checks)
    assert any(check.name == "partial_receipt" for check in checks)


def test_workflow_comparison_passes_a_fully_received_matching_total():
    invoice, purchase_order, receipt, po_line, invoice_line = _records(
        invoice_qty="10", accepted_qty="10"
    )

    checks = compare_available_records(
        invoice=invoice,
        purchase_order=purchase_order,
        receipts=[receipt],
        po_lines=[po_line],
        invoice_lines=[invoice_line],
    )

    assert checks
    assert all(check.outcome == "passed" for check in checks)
    assert any(check.name == "document_total" for check in checks)


def test_workflow_comparison_flags_invoice_quantity_above_accepted_receipt():
    checks = _checks(invoice_qty="5", accepted_qty="4")

    assert any(
        check.name == "invoice_vs_accepted_quantity" and check.outcome == "mismatch"
        for check in checks
    )


def test_workflow_comparison_flags_unit_price_mismatch():
    checks = _checks(invoice_price="11")

    assert any(
        check.name == "unit_price" and check.outcome == "mismatch" for check in checks
    )


def test_workflow_unit_price_at_exactly_two_percent_passes():
    checks = _checks(invoice_price="10.2")
    assert (
        next(check for check in checks if check.name == "unit_price").outcome
        == "passed"
    )


def test_workflow_unit_price_above_two_percent_is_issue():
    checks = _checks(invoice_price="10.201")
    assert (
        next(check for check in checks if check.name == "unit_price").outcome
        == "mismatch"
    )


def test_workflow_tax_difference_is_issue():
    invoice, po, receipt, po_line, invoice_line = _records()
    invoice.tax_amount = Decimal("2")
    po.tax_amount = Decimal("1")
    checks = compare_available_records(
        invoice=invoice,
        purchase_order=po,
        receipts=[receipt],
        po_lines=[po_line],
        invoice_lines=[invoice_line],
    )
    assert next(check for check in checks if check.name == "tax").outcome == "mismatch"


def test_workflow_currency_mismatch_is_error_even_with_exchange_rate():
    from datetime import date

    invoice, po, receipt, po_line, invoice_line = _records()
    invoice.currency = "EUR"
    rate = SimpleNamespace(rate=Decimal("1.1"), rate_date=date(2026, 10, 20))
    query = MagicMock()
    query.filter.return_value.order_by.return_value.first.return_value = rate
    db = MagicMock()
    db.query.return_value = query
    checks = compare_available_records(
        db=db,
        invoice=invoice,
        purchase_order=po,
        receipts=[receipt],
        po_lines=[po_line],
        invoice_lines=[invoice_line],
    )
    currency_check = next(check for check in checks if check.name == "currency")
    assert currency_check.outcome == "mismatch"
    assert currency_check.severity == "error"
    assert currency_check.reason == "Currency mismatch: invoice EUR, PO USD"
    assert currency_check.actual == "EUR"
    assert currency_check.expected == "USD"


def test_invoice_item_code_mismatch_is_not_linked_by_matching_description():
    invoice, purchase_order, receipt, po_line, invoice_line = _records()
    invoice_line.po_line_id = None
    invoice_line.item_code = "DIFFERENT-SKU"

    checks = compare_available_records(
        invoice=invoice,
        purchase_order=purchase_order,
        receipts=[receipt],
        po_lines=[po_line],
        invoice_lines=[invoice_line],
    )

    assert any(
        check.name == "invoice_line_link" and check.outcome == "not_possible"
        for check in checks
    )


def test_workflow_comparison_does_not_pass_missing_units_as_equal():
    invoice, purchase_order, receipt, po_line, invoice_line = _records()
    po_line.uom = None

    checks = compare_available_records(
        invoice=invoice,
        purchase_order=purchase_order,
        receipts=[receipt],
        po_lines=[po_line],
        invoice_lines=[invoice_line],
    )

    assert any(
        check.name == "unit_of_measure" and check.outcome == "not_possible"
        for check in checks
    )


def test_workflow_evaluates_the_same_complete_match_from_any_upload_anchor():
    session, _rows, _sources, invoice_doc, po_doc, gr_doc, *_ = _workflow_session()

    for document in (invoice_doc, po_doc, gr_doc):
        result = evaluate_document_workflow(session, document.id)
        assert result.status == "matched"
        assert result.missing_document_types == []


def test_invoice_and_po_without_receipt_are_a_partial_comparison():
    session, _rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session(
        with_receipt=False
    )

    result = evaluate_document_workflow(session, invoice_doc.id)

    assert result.status == "review_required"
    assert result.missing_document_types == ["goods_receipt"]
    assert result.human_decision_required is True
    assert any(check.name == "unit_price" for check in result.checks)


def test_invoice_and_extracted_receipt_without_po_are_partial():
    session, _rows, _sources, _invoice_doc, _po_doc, gr_doc, *_ = _workflow_session(
        with_po=False
    )

    result = evaluate_document_workflow(session, gr_doc.id)

    assert result.status == "partial_match"
    assert result.missing_document_types == ["po"]
    assert any(
        check.name == "invoice_vs_accepted_quantity" and check.outcome == "passed"
        for check in result.checks
    )


def test_only_invoice_awaits_related_documents():
    session, _rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session(
        with_po=False, with_receipt=False
    )

    result = evaluate_document_workflow(session, invoice_doc.id)

    assert result.status == "awaiting_related_documents"
    assert result.missing_document_types == ["po", "goods_receipt"]


def test_no_persisted_related_records_remains_awaiting_documents():
    session, rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session(
        with_po=False,
        with_receipt=False,
    )
    rows[Invoice] = []

    result = evaluate_document_workflow(session, invoice_doc.id)

    assert result.status == "awaiting_related_documents"
    assert result.missing_document_types == ["po", "goods_receipt"]


def test_purchase_order_and_receipt_without_invoice_are_partial():
    session, _rows, _sources, _invoice_doc, po_doc, _gr_doc, *_ = _workflow_session(
        with_invoice=False
    )

    result = evaluate_document_workflow(session, po_doc.id)

    assert result.status == "partial_match"
    assert result.missing_document_types == ["invoice"]


def test_multiple_purchase_order_candidates_require_human_decision():
    session, _rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session(
        with_po=True,
        with_receipt=False,
        duplicate_po=True,
    )
    invoice = session.records[Invoice][0]
    invoice.po_id = None

    result = evaluate_document_workflow(session, invoice_doc.id)

    assert result.status == "awaiting_human_decision"
    assert len(result.candidates) == 2
    assert result.human_decision_required is True


def test_multiple_invoice_candidates_are_returned_for_human_selection():
    session, rows, _sources, _invoice_doc, po_doc, _gr_doc, *_ = _workflow_session(
        with_receipt=False
    )
    invoice = rows[Invoice][0]
    rows[Invoice].append(
        SimpleNamespace(
            **{
                **invoice.__dict__,
                "id": uuid.uuid4(),
                "invoice_no": "INV-2",
                "source_document_id": uuid.uuid4(),
            }
        )
    )

    result = evaluate_document_workflow(session, po_doc.id)

    assert result.status == "awaiting_human_decision"
    invoice_candidates = [
        candidate
        for candidate in result.candidates
        if candidate.record_type == "invoice"
    ]
    assert len(invoice_candidates) == 2


def test_workflow_requires_review_for_vendor_mismatch():
    session, rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session()
    rows[Invoice][0].vendor_id = uuid.uuid4()

    result = evaluate_document_workflow(session, invoice_doc.id)

    assert result.status == "review_required"
    assert any(
        check.name == "vendor" and check.outcome == "mismatch"
        for check in result.checks
    )


def test_repeated_workflow_evaluation_is_deterministic():
    session, _rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session()

    first = evaluate_document_workflow(session, invoice_doc.id)
    second = evaluate_document_workflow(session, invoice_doc.id)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_later_po_and_receipt_complete_an_existing_invoice_workflow():
    session, rows, _sources, invoice_doc, _po_doc, _gr_doc, *_ = _workflow_session(
        with_po=False,
        with_receipt=False,
    )

    before = evaluate_document_workflow(session, invoice_doc.id)
    assert before.status == "awaiting_related_documents"

    _complete_session, complete_rows, _sources, _i_doc, _p_doc, _g_doc, *_ = (
        _workflow_session()
    )
    rows[PurchaseOrder] = complete_rows[PurchaseOrder]
    rows[PurchaseOrderLine] = complete_rows[PurchaseOrderLine]
    rows[GoodsReceipt] = complete_rows[GoodsReceipt]
    rows[Document].extend(
        document
        for document in complete_rows[Document]
        if document.doc_type in {"po", "goods_receipt"}
    )
    session.records = rows
    invoice = rows[Invoice][0]
    purchase_order = rows[PurchaseOrder][0]
    purchase_order.vendor_id = invoice.vendor_id
    invoice.po_id = purchase_order.id
    invoice_line = rows[InvoiceLine][0]
    invoice_line.po_line_id = rows[PurchaseOrderLine][0].id
    for receipt in rows[GoodsReceipt]:
        receipt.vendor_id = invoice.vendor_id

    after = evaluate_document_workflow(session, invoice_doc.id)

    assert after.status == "matched"
    assert after.missing_document_types == []


def test_invoice_recommendation_reports_actual_full_match_and_unknown_payment():
    session, _rows, _sources, _invoice_doc, _po_doc, _gr_doc, invoice, owner = (
        _workflow_session()
    )

    result = recommend_invoice_review(session, invoice, owner_id=owner)

    assert result.matching_status == "matched"
    assert result.processing_status == "extracted"
    assert result.approval_status == "awaiting_decision"
    assert result.payment_status == "unknown"
    assert all(check.finding_id for check in result.checks)
    assert all(check.category for check in result.checks)
    assert all(check.evidence_record_ids for check in result.checks)
    assert {record.type for record in result.supporting_records} >= {
        "invoice",
        "po",
        "goods_receipt",
    }
    assert result.next_recommended_action == (
        "Submit the invoice through the existing human approval workflow"
    )


def test_invoice_recommendation_finding_ids_are_stable_and_status_is_explicit():
    session, _rows, _sources, _doc, _po, _gr, invoice, owner = _workflow_session()

    first = recommend_invoice_review(session, invoice, owner_id=owner)
    second = recommend_invoice_review(session, invoice, owner_id=owner)

    assert [check.finding_id for check in first.checks] == [
        check.finding_id for check in second.checks
    ]
    assert {check.finding_status for check in first.checks} == {"supported"}
    assert all(
        set(check.evidence_record_ids)
        == {record.id for record in first.supporting_records}
        for check in first.checks
    )


def test_missing_invoice_confidence_forces_recommendation_review():
    session, _rows, _sources, _doc, _po, _gr, invoice, owner = _workflow_session()
    invoice.field_confidence = {}

    result = recommend_invoice_review(session, invoice, owner_id=owner)

    assert result.matching_status == "review_required"
    assert result.human_review_required is True
    assert any(
        check.name == "confidence"
        and check.reason == "Confidence unavailable, please verify"
        for check in result.checks
    )


def test_invoice_recommendation_reports_missing_receipt_as_partial():
    session, _rows, _sources, _doc, _po, _gr, invoice, owner = _workflow_session(
        with_receipt=False
    )

    result = recommend_invoice_review(session, invoice, owner_id=owner)

    assert result.matching_status == "review_required"
    assert any(
        check.name == "invoice_vs_accepted_quantity" and check.outcome == "mismatch"
        for check in result.checks
    )
    assert result.missing_document_types == ["goods_receipt"]
    assert result.human_review_required is True
    assert result.next_recommended_action == (
        "Upload or record the related goods receipt"
    )


def test_invoice_recommendation_includes_extraction_validation_issue():
    session, _rows, _sources, invoice_doc, _po, _gr, invoice, owner = (
        _workflow_session()
    )
    invoice_doc.status = "needs_data_review"
    invoice_doc.failure_message = "Invoice total is invalid"

    result = recommend_invoice_review(session, invoice, owner_id=owner)

    assert result.matching_status == "review_required"
    assert "extraction_validation_issue" in result.reason_codes
    assert any(item.name == "extraction_validation" for item in result.discrepancies)
    finding = next(
        item for item in result.checks if item.name == "extraction_validation"
    )
    assert finding.finding_status == "open"
    assert finding.category == "extraction_validation"


def test_invoice_recommendation_reports_multiple_po_candidates():
    session, _rows, _sources, _doc, _po, _gr, invoice, owner = _workflow_session(
        with_receipt=False, duplicate_po=True
    )
    invoice.po_id = None

    result = recommend_invoice_review(session, invoice, owner_id=owner)

    assert result.matching_status == "awaiting_human_decision"
    assert len(result.candidates) == 2
    assert "ambiguous_candidates" in result.reason_codes


def test_invoice_recommendation_reports_duplicate_gr_and_invoice_candidates():
    session, rows, _sources, _doc, _po, _gr, invoice, owner = _workflow_session()
    duplicate_receipt = SimpleNamespace(
        **{**rows[GoodsReceipt][0].__dict__, "id": uuid.uuid4()}
    )
    rows[GoodsReceipt].append(duplicate_receipt)
    duplicate_invoice = SimpleNamespace(
        **{**rows[Invoice][0].__dict__, "id": uuid.uuid4()}
    )
    rows[Invoice].append(duplicate_invoice)

    result = recommend_invoice_review(session, invoice, owner_id=owner)

    assert "duplicate_goods_receipt_candidate" in result.reason_codes
    assert "duplicate_invoice_candidate" in result.reason_codes
    assert any(candidate.id == duplicate_invoice.id for candidate in result.candidates)
    assert any(candidate.id == duplicate_receipt.id for candidate in result.candidates)
    assert any(
        candidate.id == duplicate_receipt.id for candidate in result.supporting_records
    )


def test_invoice_recommendation_reports_total_discrepancy_values():
    session, rows, _sources, _doc, _po, _gr, invoice, owner = _workflow_session()
    invoice.total_amount = Decimal("95.00")
    rows[InvoiceLine][0].line_amount = Decimal("100.00")

    result = recommend_invoice_review(session, invoice, owner_id=owner)
    line_total_check = next(
        item for item in result.checks if item.name == "invoice_line_total"
    )

    assert result.matching_status == "review_required"
    assert line_total_check.reason_code == "line_total_discrepancy"
    assert line_total_check.finding_status == "open"
    assert line_total_check.category == "financial"
    assert line_total_check.expected == Decimal("95.00")
    assert line_total_check.actual == Decimal("100.00")
    assert line_total_check.difference == Decimal("5.00")
