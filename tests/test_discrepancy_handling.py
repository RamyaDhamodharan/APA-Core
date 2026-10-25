import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.invoice_issue_models import InvoiceIssue
from src.models.invoice_match_models import InvoiceDiscrepancy
from src.models.invoice_models import Invoice
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder

client = TestClient(app)


def test_discrepancy_endpoints_flow():
    invoice_id = uuid.uuid4()
    user_id = uuid.uuid4()
    po_id = uuid.uuid4()
    run_id = uuid.uuid4()
    disc1_id = uuid.uuid4()
    disc2_id = uuid.uuid4()

    invoice = Invoice(
        id=invoice_id,
        status="needs_review",
        workflow_status="match_issues",
        match_status="match_issues",
        po_id=po_id,
        final_run_id=run_id,
        owner_user_id=user_id,
    )
    po = PurchaseOrder(id=po_id, po_number="PO-2026-001")
    run = MatchRun(
        id=run_id,
        invoice_id=invoice_id,
        run_no=1,
        result="match_issues",
        differences=[
            {"code": "PRICE_VARIANCE", "message": "Price differs", "invoice_value": 110, "expected_value": 100},
            {"code": "MISSING_GR", "message": "No goods receipt", "invoice_value": None, "expected_value": None},
        ],
        rules_used={"match_outcome": {"outcome": "needs_review", "is_match": False, "discrepancies": [{"code": "PRICE_VARIANCE"}]}},
    )
    disc1 = InvoiceDiscrepancy(
        id=disc1_id,
        invoice_id=invoice_id,
        field="price_variance",
        invoice_value=110,
        po_value=100,
        severity="error",
        resolved=False,
    )
    disc2 = InvoiceDiscrepancy(
        id=disc2_id,
        invoice_id=invoice_id,
        field="missing_gr",
        invoice_value=None,
        po_value=None,
        severity="error",
        resolved=False,
    )
    issue = InvoiceIssue(
        id=uuid.uuid4(),
        invoice_id=invoice_id,
        issue_type="price_variance",
        severity="error",
        description="Price variance detected",
        status="open",
    )

    db_session = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "role": "admin"}
    app.dependency_overrides[get_db] = lambda: db_session

    try:
        def mock_get(model, pk):
            if model is Invoice and pk == invoice_id:
                return invoice
            if model is MatchRun and pk == run_id:
                return run
            if model is PurchaseOrder and pk == po_id:
                return po
            return None

        db_session.get.side_effect = mock_get

        def smart_query(*args, **kwargs):
            q = MagicMock()
            q.filter.return_value = q
            q.outerjoin.return_value = q
            q.order_by.return_value = q
            q.with_for_update.return_value = q

            target_model = args[0] if args else None
            if target_model is Invoice:
                q.first.return_value = invoice
            elif target_model is InvoiceDiscrepancy:
                q.first.return_value = disc1
                q.all.return_value = [disc1, disc2]
                q.scalar.return_value = 1
            elif target_model is InvoiceIssue:
                q.all.return_value = [issue]
            else:
                q.first.return_value = None
                q.all.return_value = []
                q.scalar.return_value = 0
            return q

        db_session.query.side_effect = smart_query

        # 1. Test listing discrepancies
        res = client.get(f"/api/invoices/{invoice_id}/discrepancies")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 2

        # 2. Test auto-handle on disc1 (PRICE_VARIANCE)
        res = client.post(f"/api/invoices/{invoice_id}/discrepancies/{disc1_id}/auto-handle")
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["success"] is True
        assert disc1.resolved is True
        assert disc1.reason is not None

        # 3. Test reopen disc1
        res = client.post(f"/api/invoices/{invoice_id}/discrepancies/{disc1_id}/reopen")
        assert res.status_code == 200
        assert res.json()["resolved"] is False
        assert disc1.resolved is False

        # 4. Test resolve-all
        res = client.post(
            f"/api/invoices/{invoice_id}/discrepancies/resolve-all",
            json={"reason": "All variances verified and approved"},
        )
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["success"] is True
        assert res_data["resolved_count"] == 2
        assert disc1.resolved is True
        assert disc2.resolved is True
        assert invoice.status == "ready_to_pay"
        assert run.result == "match_ok"
        assert run.rules_used["match_outcome"]["outcome"] == "ready_to_pay"
    finally:
        app.dependency_overrides.clear()
