"""Opt-in integration fixtures. Never point these tests at a non-ap_test database."""

import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command

CORE_ROOT = Path(__file__).resolve().parents[2]
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))


@pytest.fixture(scope="session")
def test_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset; DB integration tests skipped")
    if make_url(url).database != "ap_test":
        pytest.fail("TEST_DATABASE_URL must target database named exactly ap_test")

    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        config = Config(str(CORE_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine):
    connection = test_engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture
def db_factories(db_session):
    from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
    from src.models.invoice_models import Invoice, InvoiceLine
    from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
    from src.models.user_models import User
    from src.models.vendor_models import Vendor

    def persist(row):
        db_session.add(row)
        db_session.flush()
        return row

    def vendor(**values):
        return persist(
            Vendor(name=values.pop("name", f"Vendor {uuid.uuid4()}"), **values)
        )

    def user(role="associate", **values):
        return persist(
            User(
                name=values.pop("name", role.title()),
                email=values.pop("email", f"{role}-{uuid.uuid4()}@example.test"),
                role=role,
                **values,
            )
        )

    def purchase_order(vendor_row, *, lines=None, **values):
        row = PurchaseOrder(
            po_number=values.pop("po_number", f"PO-{uuid.uuid4()}"),
            vendor_id=vendor_row.id,
            currency=values.pop("currency", "USD"),
            **values,
        )
        row.lines = [PurchaseOrderLine(**line) for line in (lines or [])]
        return persist(row)

    def goods_receipt(po, *, lines=None, **values):
        row = GoodsReceipt(
            gr_number=values.pop("gr_number", f"GR-{uuid.uuid4()}"),
            po_id=po.id,
            vendor_id=po.vendor_id,
            receipt_date=values.pop("receipt_date", date.today()),
            **values,
        )
        row.lines = [GoodsReceiptLine(**line) for line in (lines or [])]
        return persist(row)

    def invoice(vendor_row=None, *, lines=None, **values):
        row = Invoice(
            vendor_id=vendor_row.id if vendor_row else None,
            invoice_no=values.pop("invoice_no", f"INV-{uuid.uuid4()}"),
            currency=values.pop("currency", "USD"),
            **values,
        )
        persist(row)
        for line in lines or []:
            persist(InvoiceLine(invoice_id=row.id, **line))
        return row

    return {
        "vendor": vendor,
        "purchase_order": purchase_order,
        "goods_receipt": goods_receipt,
        "invoice": invoice,
        "admin": lambda **values: user("admin", **values),
        "associate": lambda **values: user("associate", **values),
    }
