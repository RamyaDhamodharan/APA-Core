import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.schemas.goods_receipt_schemas import GoodsReceiptLineResponse


def test_goods_receipt_list_keeps_array_and_adds_total_and_filters():
    owner_id = uuid.uuid4()
    now = datetime.now(UTC)
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        gr_number="GR-101",
        po_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        source_document_id=uuid.uuid4(),
        receipt_date=date(2026, 10, 1),
        created_at=now,
        updated_at=now,
        lines=[],
    )
    db = MagicMock()
    db.scalar.return_value = 1
    db.scalars.return_value.all.return_value = [receipt]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    try:
        response = TestClient(app).get(
            "/api/goods-receipts",
            params={
                "source_document_id": str(receipt.source_document_id),
                "search": "GR-10",
                "sort_by": "gr_number",
                "sort_dir": "asc",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["gr_number"] == "GR-101"
    assert response.headers["x-total-count"] == "1"
    assert "goods_receipts.created_by" in str(db.scalar.call_args.args[0])
    assert "documents.created_by" in str(db.scalar.call_args.args[0])


def test_goods_receipt_response_allows_unverified_none_quantities():
    row = GoodsReceiptLineResponse(
        id=uuid.uuid4(),
        po_line_id=None,
        received_qty=None,
        accepted_qty=None,
        rejected_qty=None,
    )
    assert row.received_qty is None
