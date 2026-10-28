"""
Comprehensive test suite verifying that:
1. All non-calculation matching intelligence is handled by the LLM.
2. All numerical calculations and arithmetic validations remain deterministic Python code.
3. The LLM is never trusted to calculate financial values or approve payments.
4. User/tenant data isolation and API compatibility are strictly preserved.

Covers all 14 required cases from the refactor specification.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor
from src.services import ap_decision_engine as engine
from src.services import invoice_matching_services as matching
from src.services import llm_matching_services as llm_service


def _build_test_entities(
    *,
    owner_id=None,
    invoice_qty="10",
    invoice_price="10.00",
    invoice_amount="100.00",
    invoice_subtotal="100.00",
    invoice_tax="0.00",
    invoice_total="100.00",
    po_qty="10",
    po_price="10.00",
    po_tax="0.00",
    po_total="100.00",
    gr_accepted="10",
    invoice_item_code="SKU-1",
    invoice_description="Widget A",
    po_item_code="SKU-1",
    po_description="Widget A",
    currency="USD",
    po_currency="USD",
    vendor_id=None,
    po_vendor_id=None,
    has_po=True,
    has_gr=True,
):
    inv_id = uuid.uuid4()
    p_id = uuid.uuid4() if has_po else None
    v_id = vendor_id or uuid.uuid4()
    pv_id = po_vendor_id or v_id
    pl_id = uuid.uuid4()
    gr_id = uuid.uuid4() if has_gr else None

    po = None
    po_line = None
    if has_po:
        po = SimpleNamespace(
            id=p_id,
            po_number="PO-100",
            vendor_id=pv_id,
            vendor=SimpleNamespace(id=pv_id, name="Test Vendor Corp", tax_id="TAX-123"),
            currency=po_currency,
            subtotal=Decimal(po_total) - Decimal(po_tax),
            tax_amount=Decimal(po_tax),
            total_amount=Decimal(po_total),
            is_current=True,
            created_by=owner_id,
        )
        po_line = SimpleNamespace(
            id=pl_id,
            po_id=p_id,
            item_code=po_item_code,
            description=po_description,
            qty=Decimal(po_qty),
            unit_price=Decimal(po_price),
            line_amount=Decimal(po_qty) * Decimal(po_price),
        )
        po.lines = [po_line]

    gr = None
    gr_line = None
    if has_gr and has_po:
        gr = SimpleNamespace(
            id=gr_id,
            gr_number="GR-100",
            po_id=p_id,
            vendor_id=pv_id,
            vendor=SimpleNamespace(id=pv_id, name="Test Vendor Corp"),
            receipt_date=None,
            created_by=owner_id,
        )
        gr_line = SimpleNamespace(
            id=uuid.uuid4(),
            goods_receipt_id=gr_id,
            po_line_id=pl_id,
            item_code=po_item_code,
            description=po_description,
            received_qty=Decimal(gr_accepted),
            accepted_qty=Decimal(gr_accepted),
            rejected_qty=Decimal("0"),
        )
        gr.lines = [gr_line]

    inv_line = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=inv_id,
        item_code=invoice_item_code,
        description=invoice_description,
        qty=Decimal(invoice_qty) if invoice_qty is not None else None,
        unit_price=Decimal(invoice_price) if invoice_price is not None else None,
        line_amount=Decimal(invoice_amount) if invoice_amount is not None else None,
        field_confidence={k: {"confidence": 95} for k in ("qty", "unit_price", "line_amount")},
    )

    invoice = SimpleNamespace(
        id=inv_id,
        invoice_no="INV-100",
        invoice_no_normalized="INV100",
        vendor_id=v_id,
        vendor_name_raw="Test Vendor Corp",
        vendor_tax_id_raw=None,
        vendor=SimpleNamespace(id=v_id, name="Test Vendor Corp"),
        po_id=p_id,
        po_number_raw="PO-100" if has_po else None,
        currency=currency,
        subtotal=Decimal(invoice_subtotal) if invoice_subtotal is not None else None,
        tax_amount=Decimal(invoice_tax) if invoice_tax is not None else None,
        total_amount=Decimal(invoice_total) if invoice_total is not None else None,
        field_confidence={field: {"confidence": 95} for field in engine.MATERIAL_FIELDS},
        overall_confidence_percent=95,
        status="processing",
        workflow_status="matching",
        match_status="pending",
        final_status=None,
        final_run_id=None,
        created_by=owner_id,
        source_document_id=None,
    )

    datasets = {
        PurchaseOrder: [po] if po else [],
        PurchaseOrderLine: [po_line] if po_line else [],
        GoodsReceipt: [gr] if gr else [],
        GoodsReceiptLine: [gr_line] if gr_line else [],
        InvoiceLine: [inv_line],
    }

    db = MagicMock()
    db.get.side_effect = lambda model, ident: (
        invoice if model == Invoice and ident == inv_id
        else po if model == PurchaseOrder and ident == p_id
        else None
    )

    def mock_query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.options.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        res = datasets.get(model, [])
        q.all.return_value = list(res)
        q.first.return_value = res[0] if res else None
        q.scalar.return_value = 0
        return q

    db.query.side_effect = mock_query
    return db, invoice, po, po_line, gr, gr_line, inv_line


# ---------------------------------------------------------------------------
# Test 1: LLM selects a valid PO and deterministic calculations pass
# ---------------------------------------------------------------------------


def test_01_llm_selects_valid_po_and_deterministic_calculations_pass(monkeypatch):
    """When LLM proposes a valid PO and Python verifies all arithmetic and tolerances, invoice becomes ready_to_pay."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities()

    # LLM proposes semantic relationship
    llm_proposal = {
        "matched_po_id": str(po.id),
        "matched_po_number": po.po_number,
        "matched_gr_id": str(gr.id),
        "resolved_vendor_name": "Test Vendor Corp",
        "line_mappings": [
            {
                "invoice_line_id": str(inv_line.id),
                "matched_po_line_id": str(po_line.id),
                "match_confidence": 98,
                "reasoning": "Identical product SKU and description",
            }
        ],
        "summary": "Clean semantic match to PO-100",
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "ready_to_pay"
    assert decision["is_match"] is True
    assert decision["match_score"] == 100
    assert decision["discrepancies"] == []

    # Verify matching workflow status
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    run = matching.run_match_for_invoice(db, invoice.id, force=True)
    assert run.result == "match_ok"
    assert invoice.status == "ready_to_pay"
    assert invoice.workflow_status == "match_approved"


# ---------------------------------------------------------------------------
# Test 2: LLM proposes the wrong or nonexistent PO ID
# ---------------------------------------------------------------------------


def test_02_llm_proposes_wrong_or_nonexistent_po_id(monkeypatch):
    """When LLM hallucinates a nonexistent PO ID, Python validates against DB, rejects it, and marks for review."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(has_po=False)

    fake_po_id = str(uuid.uuid4())
    llm_proposal = {
        "matched_po_id": fake_po_id,
        "matched_po_number": "PO-GHOST-999",
        "line_mappings": [],
        "summary": "Proposing nonexistent PO",
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "no_match"
    assert decision["is_match"] is False
    assert any(d["code"] == "MISSING_PO" for d in decision["discrepancies"])

    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    run = matching.run_match_for_invoice(db, invoice.id, force=True)
    assert run.result == "no_match"
    assert invoice.status == "needs_review"


# ---------------------------------------------------------------------------
# Test 3: Invoice description differs from PO description but is semantically equivalent
# ---------------------------------------------------------------------------


def test_03_invoice_description_differs_from_po_but_semantically_equivalent(monkeypatch):
    """LLM resolves differing descriptions semantically; Python verifies financial calculations and approves."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_item_code=None,  # No SKU on invoice
        invoice_description="Executive ergonomic mesh chair - midnight black",
        po_item_code="CHAIR-BLK",
        po_description="Standard Ergonomic Task Chair (Black)",
    )

    llm_proposal = {
        "matched_po_id": str(po.id),
        "matched_po_number": po.po_number,
        "matched_gr_id": str(gr.id),
        "line_mappings": [
            {
                "invoice_line_id": str(inv_line.id),
                "matched_po_line_id": str(po_line.id),
                "match_confidence": 95,
                "reasoning": "Both describe black ergonomic task chairs",
            }
        ],
        "summary": "Matched line items semantically by product description",
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "ready_to_pay"
    assert decision["is_match"] is True
    # Verify no LINE_NOT_ON_PO discrepancy
    assert not any(d["code"] == "LINE_NOT_ON_PO" for d in decision["discrepancies"])


# ---------------------------------------------------------------------------
# Test 4: Price variance exceeds configured tolerance
# ---------------------------------------------------------------------------


def test_04_price_variance_exceeds_configured_tolerance(monkeypatch):
    """Even if LLM claims a match, Python detects unit price exceeding 2% tolerance and forces needs_review."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_price="10.50",  # 5% higher than PO unit price of 10.00
        invoice_amount="105.00",
        invoice_subtotal="105.00",
        invoice_total="105.00",
        po_price="10.00",
    )

    # LLM incorrectly claims everything is fine
    llm_proposal = {
        "matched_po_id": str(po.id),
        "is_match": True,
        "outcome": "ready_to_pay",
        "match_score": 100,
        "line_mappings": [{"invoice_line_id": str(inv_line.id), "matched_po_line_id": str(po_line.id)}],
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "needs_review"
    assert decision["is_match"] is False
    price_issues = [d for d in decision["discrepancies"] if d["code"] == "PRICE_VARIANCE"]
    assert len(price_issues) == 1
    assert Decimal(price_issues[0]["difference"]) == Decimal("0.50")
    assert Decimal(price_issues[0]["tolerance"]) == Decimal("0.20")

    # Workflow status enforcement
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    run = matching.run_match_for_invoice(db, invoice.id, force=True)
    assert run.result == "match_issues"
    assert invoice.status == "needs_review"


# ---------------------------------------------------------------------------
# Test 5: Invoice quantity exceeds accepted Goods Receipt quantity
# ---------------------------------------------------------------------------


def test_05_invoice_quantity_exceeds_accepted_goods_receipt_quantity(monkeypatch):
    """Even if LLM claims a match, Python detects billed qty > accepted GR qty and flags QUANTITY_VARIANCE."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_qty="10",
        gr_accepted="6",  # Only 6 accepted on goods receipt
    )

    llm_proposal = {
        "matched_po_id": str(po.id),
        "is_match": True,
        "outcome": "ready_to_pay",
        "line_mappings": [{"invoice_line_id": str(inv_line.id), "matched_po_line_id": str(po_line.id)}],
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "needs_review"
    qty_issues = [d for d in decision["discrepancies"] if d["code"] == "QUANTITY_VARIANCE"]
    assert len(qty_issues) == 1
    assert Decimal(qty_issues[0]["invoice_value"]) == Decimal("10")
    assert Decimal(qty_issues[0]["expected_value"]) == Decimal("6")
    assert Decimal(qty_issues[0]["difference"]) == Decimal("4")


# ---------------------------------------------------------------------------
# Test 6: Inconsistent line total, subtotal, tax, or total calculation
# ---------------------------------------------------------------------------


def test_06_inconsistent_line_total_subtotal_tax_or_total_calculations(monkeypatch):
    """Python arithmetic checks detect line total, subtotal, and total inconsistencies deterministically."""
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    # 6a. Line total arithmetic failure (10 * 10 = 100 != 120)
    db1, inv1, po1, _, _, _, _ = _build_test_entities(invoice_qty="10", invoice_price="10.00", invoice_amount="120.00")
    dec1 = engine.evaluate_invoice(db1, inv1)
    assert any(d["code"] == "LINE_TOTAL_VARIANCE" for d in dec1["discrepancies"])
    assert dec1["outcome"] == "needs_review"

    # 6b. Subtotal mismatch (sum of lines 100 != stated subtotal 110)
    db2, inv2, po2, _, _, _, _ = _build_test_entities(invoice_amount="100.00", invoice_subtotal="110.00")
    dec2 = engine.evaluate_invoice(db2, inv2)
    assert any(d["code"] == "HEADER_TOTAL_VARIANCE" for d in dec2["discrepancies"])

    # 6c. Header total mismatch (subtotal 100 + tax 0 = 100 != stated total 125)
    db3, inv3, po3, _, _, _, _ = _build_test_entities(invoice_subtotal="100.00", invoice_tax="0.00", invoice_total="125.00")
    dec3 = engine.evaluate_invoice(db3, inv3)
    assert any(d["code"] == "HEADER_TOTAL_VARIANCE" for d in dec3["discrepancies"])

    # 6d. Tax variance (invoice tax 15.00 vs PO tax 0.00 exceeds tolerance 0.01)
    db4, inv4, po4, _, _, _, _ = _build_test_entities(invoice_tax="15.00", invoice_total="115.00", po_tax="0.00")
    dec4 = engine.evaluate_invoice(db4, inv4)
    assert any(d["code"] == "TAX_VARIANCE" for d in dec4["discrepancies"])


# ---------------------------------------------------------------------------
# Test 7: Missing Goods Receipt
# ---------------------------------------------------------------------------


def test_07_missing_goods_receipt(monkeypatch):
    """When PO is valid but no Goods Receipt exists in DB, Python flags MISSING_GR and blocks payment."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(has_gr=False)

    llm_proposal = {
        "matched_po_id": str(po.id),
        "is_match": True,
        "outcome": "ready_to_pay",
        "summary": "PO matched but no receipt",
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "needs_review"
    assert any(d["code"] == "MISSING_GR" for d in decision["discrepancies"])


# ---------------------------------------------------------------------------
# Test 8: Missing or ambiguous PO relationship
# ---------------------------------------------------------------------------


def test_08_missing_or_ambiguous_po_relationship(monkeypatch):
    """Python detects missing PO or multiple duplicate PO records deterministically."""
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    # 8a. Missing PO reference
    db1, inv1, _, _, _, _, _ = _build_test_entities(has_po=False)
    inv1.po_number_raw = None
    dec1 = engine.evaluate_invoice(db1, inv1)
    assert dec1["outcome"] == "no_match"
    assert any(d["code"] == "MISSING_PO" for d in dec1["discrepancies"])

    # 8b. Ambiguous duplicate POs in DB
    db2, inv2, po2, _, _, _, _ = _build_test_entities()
    inv2.po_id = None
    duplicate_po = SimpleNamespace(
        id=uuid.uuid4(),
        po_number=po2.po_number,
        is_current=True,
    )
    # Return multiple POs for same number
    datasets = {
        PurchaseOrder: [po2, duplicate_po],
        InvoiceLine: [inv2],
    }
    def mock_dup_query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.options.return_value = q
        q.order_by.return_value = q
        res = datasets.get(model, [po2, duplicate_po] if model == PurchaseOrder else [])
        q.all.return_value = res
        q.first.return_value = res[0] if res else None
        return q
    db2.query.side_effect = mock_dup_query
    dec2 = engine.evaluate_invoice(db2, inv2)
    assert dec2["outcome"] == "needs_review"
    assert any(d["code"] == "AMBIGUOUS_PO" for d in dec2["discrepancies"])


# ---------------------------------------------------------------------------
# Test 9: Duplicate invoice
# ---------------------------------------------------------------------------


def test_09_duplicate_invoice(monkeypatch):
    """When an identical invoice exists for this vendor, Python flags POSSIBLE_DUPLICATE and blocks approval."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities()

    # Simulate existing duplicate in database
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: object())
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "needs_review"
    assert any(d["code"] == "POSSIBLE_DUPLICATE" for d in decision["discrepancies"])


# ---------------------------------------------------------------------------
# Test 10: LLM timeout, malformed output, and complete provider failure
# ---------------------------------------------------------------------------


def test_10_llm_timeout_malformed_output_and_complete_provider_failure(monkeypatch):
    """When all LLM providers timeout or return malformed JSON, system handles errors gracefully without crash."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities()

    def mock_timeout_post(*args, **kwargs):
        raise httpx.TimeoutException("LLM provider timed out")

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm_service.httpx, "post", mock_timeout_post)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    # Must not raise an exception; falls back to deterministic Python evaluation
    decision = engine.evaluate_invoice(db, invoice)
    assert decision is not None
    # Because exact SKU and PO exist on DB records, deterministic checks succeed
    assert decision["outcome"] == "ready_to_pay"


# ---------------------------------------------------------------------------
# Test 11: LLM says "match" while deterministic calculations identify blocking discrepancy
# ---------------------------------------------------------------------------


def test_11_llm_says_match_while_deterministic_calculations_identify_blocking_discrepancy(monkeypatch):
    """LLM claim of 'ready_to_pay' is strictly overridden by Python when financial calculations fail."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_price="20.00",  # 100% price variance
        invoice_amount="200.00",
        invoice_subtotal="200.00",
        invoice_total="200.00",
        po_price="10.00",
    )

    llm_unsupported_claim = {
        "matched_po_id": str(po.id),
        "is_match": True,
        "outcome": "ready_to_pay",
        "match_score": 100,
        "summary": "LLM falsely claims perfect match!",
        "line_mappings": [{"invoice_line_id": str(inv_line.id), "matched_po_line_id": str(po_line.id)}],
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_unsupported_claim)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    # Python must strictly override the LLM's claim
    assert decision["outcome"] == "needs_review"
    assert decision["is_match"] is False
    assert any(d["code"] == "PRICE_VARIANCE" for d in decision["discrepancies"])

    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    run = matching.run_match_for_invoice(db, invoice.id, force=True)
    assert run.result == "match_issues"
    assert invoice.status == "needs_review"


# ---------------------------------------------------------------------------
# Test 12: LLM says "no match" or returns uncertain results
# ---------------------------------------------------------------------------


def test_12_llm_says_no_match_or_returns_uncertain_results(monkeypatch):
    """When LLM returns no match or uncertain results, invoice is held for human review."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(has_po=False)

    llm_uncertain = {
        "matched_po_id": None,
        "matched_po_number": None,
        "is_match": False,
        "outcome": "no_match",
        "summary": "Could not identify corresponding purchase order",
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: llm_uncertain)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    assert decision["outcome"] == "no_match"
    assert decision["is_match"] is False
    assert decision["match_score"] == 0


# ---------------------------------------------------------------------------
# Test 13: User / Tenant Data Isolation
# ---------------------------------------------------------------------------


def test_13_user_tenant_data_isolation(monkeypatch):
    """Cross-tenant PO proposing is blocked: records outside user's permitted scope are rejected."""
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    # User A's invoice
    db, invoice, _, _, _, _, _ = _build_test_entities(owner_id=user_a_id, has_po=False)

    # PO owned by User B
    foreign_po_id = uuid.uuid4()
    foreign_po = SimpleNamespace(
        id=foreign_po_id,
        po_number="PO-USER-B",
        is_current=True,
        created_by=user_b_id,
    )

    # Mock DB query with owner scoping
    def scoped_query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        # When scoped by user_a_id, foreign_po is NOT returned
        q.all.return_value = []
        q.first.return_value = None
        return q

    db.query.side_effect = scoped_query

    # LLM attempts to propose User B's PO for User A's invoice
    cross_tenant_proposal = {
        "matched_po_id": str(foreign_po_id),
        "matched_po_number": "PO-USER-B",
        "line_mappings": [],
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: cross_tenant_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice, owner_id=user_a_id)

    # Foreign PO proposal is rejected due to owner_scope validation
    assert decision["outcome"] == "no_match"
    assert any(d["code"] == "MISSING_PO" for d in decision["discrepancies"])


# ---------------------------------------------------------------------------
# Test 14: Existing API response and workflow status compatibility
# ---------------------------------------------------------------------------


def test_14_existing_api_response_and_workflow_status_compatibility(monkeypatch):
    """Verify all expected dictionary keys, discrepancy shapes, and lifecycle transitions."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities()

    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    # Check top-level contract keys
    expected_top_keys = {
        "rule_set_version",
        "outcome",
        "confidence_level",
        "overall_confidence_percent",
        "field_confidence",
        "confidence_levels",
        "low_confidence_fields",
        "discrepancies",
        "summary",
        "inputs_summary",
        "validation_report",
    }
    assert expected_top_keys <= set(decision.keys())

    # Check inputs_summary contract keys
    expected_input_keys = {
        "invoice_id",
        "po_number",
        "vendor_id",
        "invoice_number",
        "currency",
        "invoice_total",
        "invoice_subtotal",
        "invoice_tax",
        "po_ids",
        "receipt_ids",
        "po_currency",
        "po_tax",
        "po_total",
        "invoice_lines",
        "po_lines",
        "accepted_quantity_by_po_line",
    }
    assert expected_input_keys <= set(decision["inputs_summary"].keys())

    # Check MatchRun creation and audit trail integration
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    run = matching.run_match_for_invoice(db, invoice.id, force=True)

    assert isinstance(run, MatchRun)
    assert run.result == "match_ok"
    assert "rule_set_version" in run.rules_used
    assert "match_outcome" in run.rules_used
    assert "inputs_summary" in run.rules_used
    assert invoice.status == "ready_to_pay"
    assert invoice.workflow_status == "match_approved"


# ---------------------------------------------------------------------------
# Test 15: Quantity correctness in 3-way and 2-way contexts
# ---------------------------------------------------------------------------


def test_15_quantity_correctness_and_zero_discrepancy_when_matching(monkeypatch):
    """Verify quantity checking is exact and discrepancy shows ONLY when there is a true difference."""
    # 1. Matching quantities: 10 billed <= 10 accepted -> 0 discrepancies
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_qty="10",
        gr_accepted="10",
        po_qty="10",
    )
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)
    assert decision["outcome"] == "ready_to_pay"
    assert len(decision["discrepancies"]) == 0

    # 2. Billed exceeds accepted: 12 billed > 10 accepted -> QUANTITY_VARIANCE
    db2, invoice2, po2, po_line2, gr2, gr_line2, inv_line2 = _build_test_entities(
        invoice_qty="12",
        invoice_amount="120.00",
        invoice_subtotal="120.00",
        invoice_total="120.00",
        gr_accepted="10",
    )
    decision2 = engine.evaluate_invoice(db2, invoice2)
    qty_issues = [d for d in decision2["discrepancies"] if d["code"] == "QUANTITY_VARIANCE"]
    assert len(qty_issues) == 1
    assert qty_issues[0]["invoice_value"] == "12"
    assert qty_issues[0]["expected_value"] == "10"
    assert qty_issues[0]["difference"] == "2.00"


# ---------------------------------------------------------------------------
# Test 16: Discrepancy shows ONLY when there is an actual discrepancy
# ---------------------------------------------------------------------------


def test_16_discrepancy_shows_only_when_actual_discrepancy_exists(monkeypatch):
    """Verify zero false-positive discrepancies when PO tax is None and subtotal equals total."""
    # PO has no tax recorded (tax_amount is None), invoice has 0.00 tax -> NO TAX_VARIANCE
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_tax="0.00",
        po_tax="0.00",
    )
    po.tax_amount = None  # Common in systems where POs don't store tax
    invoice.tax_amount = None

    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)
    assert decision["outcome"] == "ready_to_pay"
    assert len(decision["discrepancies"]) == 0


# ---------------------------------------------------------------------------
# Test 17: Anti-Hallucination: LLM cannot hallucinate false discrepancies
# ---------------------------------------------------------------------------


def test_17_llm_hallucination_prevention_for_discrepancies(monkeypatch):
    """Verify that if the LLM hallucinates fake discrepancies in its output, Python ignores them if calculations pass."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities()

    # LLM hallucinates fake price and quantity variances in its response
    fake_llm_proposal = {
        "matched_po_id": str(po.id),
        "matched_po_number": po.po_number,
        "matched_gr_id": str(gr.id),
        "matched_gr_number": gr.gr_number,
        "resolved_vendor_name": "Test Vendor Corp",
        "confidence_reasoning": "Hallucinated issue proposal",
        "line_mappings": [
            {
                "invoice_line_id": str(inv_line.id),
                "matched_po_line_id": str(po_line.id),
                "match_confidence": 95,
                "reasoning": "Matching SKU-1",
            }
        ],
        "discrepancies": [
            {
                "code": "PRICE_VARIANCE",
                "message": "Hallucinated price difference that does not exist in data",
                "next_action": "Check prices",
            },
            {
                "code": "QUANTITY_VARIANCE",
                "message": "Hallucinated quantity difference that does not exist in data",
                "next_action": "Check quantities",
            },
        ],
        "summary": "LLM claimed discrepancies exist",
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: fake_llm_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    # Deterministic calculations pass, so fake LLM discrepancies are NOT in the final decision!
    assert decision["outcome"] == "ready_to_pay"
    assert len(decision["discrepancies"]) == 0


# ---------------------------------------------------------------------------
# Test 18: Anti-Hallucination: LLM line mapping with conflicting SKU is rejected
# ---------------------------------------------------------------------------


def test_18_llm_hallucination_prevention_for_conflicting_skus(monkeypatch):
    """Verify that an LLM proposal mapping an invoice SKU to a contradictory PO SKU is rejected."""
    db, invoice, po, po_line1, gr, gr_line, inv_line = _build_test_entities(
        invoice_item_code="SKU-CORRECT",
        po_item_code="SKU-CORRECT",
    )
    # Add a second PO line with a conflicting SKU
    po_line_wrong = SimpleNamespace(
        id=uuid.uuid4(),
        po_id=po.id,
        item_code="SKU-WRONG",
        description="Wrong item",
        qty=Decimal("10"),
        unit_price=Decimal("999.00"),
        line_amount=Decimal("9990.00"),
    )
    po.lines.append(po_line_wrong)

    # LLM hallucinates and maps SKU-CORRECT to SKU-WRONG
    hallucinated_proposal = {
        "matched_po_id": str(po.id),
        "matched_po_number": po.po_number,
        "matched_gr_id": str(gr.id),
        "matched_gr_number": gr.gr_number,
        "resolved_vendor_name": "Test Vendor Corp",
        "line_mappings": [
            {
                "invoice_line_id": str(inv_line.id),
                "matched_po_line_id": str(po_line_wrong.id),  # Hallucinated wrong line!
                "match_confidence": 99,
                "reasoning": "Hallucinated mapping",
            }
        ],
        "discrepancies": [],
    }
    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: hallucinated_proposal)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)

    # Python rejected the conflicting SKU mapping and deterministically matched SKU-CORRECT!
    assert decision["outcome"] == "ready_to_pay"
    assert len(decision["discrepancies"]) == 0


# ---------------------------------------------------------------------------
# Test 19: Unlinked receipt line mapped by SKU preserves quantity
# ---------------------------------------------------------------------------


def test_19_unlinked_receipt_line_mapped_by_sku_preserves_quantity(monkeypatch):
    """Verify that receipt lines with po_line_id=None are resolved by SKU, preventing false QUANTITY_VARIANCE."""
    db, invoice, po, po_line, gr, gr_line, inv_line = _build_test_entities(
        invoice_qty="10",
        gr_accepted="10",
    )
    # Unlink po_line_id on the receipt line (e.g. extracted without FK)
    gr_line.po_line_id = None
    gr_line.item_code = "SKU-1"

    monkeypatch.setattr(llm_service, "llm_propose_matches", lambda *a, **k: None)
    monkeypatch.setattr(engine, "find_duplicate_invoice", lambda *a, **k: None)

    decision = engine.evaluate_invoice(db, invoice)
    assert decision["outcome"] == "ready_to_pay"
    assert len(decision["discrepancies"]) == 0

