"""Seed deterministic, synthetic AP decision-engine examples into an allowlisted DB."""

import argparse
import os
from datetime import date
from decimal import Decimal

from sqlalchemy.engine import make_url

from src.db.session_db import DATABASE_URL, SessionLocal
from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor

CASES = (
    "perfect",
    "price_5pct",
    "qty_100_received_80",
    "no_gr",
    "no_po",
    "unknown_vendor",
    "vendor_mismatch",
    "currency_mismatch",
    "tax_mismatch",
    "header_total_mismatch",
    "line_not_on_po",
    "duplicate",
    "low_confidence",
    "multiple_discrepancies",
)
CONFIDENCE = {
    key: {"confidence": 95, "derived": False}
    for key in (
        "vendor",
        "invoice_number",
        "invoice_date",
        "currency",
        "subtotal",
        "tax",
        "total",
        "order_number",
        "gl_code",
    )
}


def _safe_database(database_url):
    if not database_url:
        raise ValueError("DATABASE_URL is not configured")
    name = str(make_url(database_url).database or "")
    allowlist = {
        item.strip()
        for item in os.getenv("EVAL_ALLOWED_DATABASES", "").split(",")
        if item.strip()
    }
    if not name or name not in allowlist:
        raise ValueError("Database name is not listed in EVAL_ALLOWED_DATABASES")


def seed():
    _safe_database(DATABASE_URL)
    with SessionLocal.begin() as db:
        vendor = db.query(Vendor).filter(Vendor.tax_id == "DEMO-AP-001").first()
        if vendor is None:
            vendor = Vendor(
                name="Synthetic Demo Supplies", tax_id="DEMO-AP-001", is_active=True
            )
            db.add(vendor)
            db.flush()
        other_vendor = db.query(Vendor).filter(Vendor.tax_id == "DEMO-AP-002").first()
        if other_vendor is None:
            other_vendor = Vendor(
                name="Synthetic Other Vendor", tax_id="DEMO-AP-002", is_active=True
            )
            db.add(other_vendor)
            db.flush()
        for index, case in enumerate(CASES, 1):
            invoice_no = f"DEMO-ENGINE-{index:02d}"
            existing = (
                db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
            )
            if existing:
                continue
            po_number = f"DEMO-PO-{index:02d}"
            has_po = case not in {"no_po"}
            po_vendor = other_vendor if case == "vendor_mismatch" else vendor
            po = None
            po_line = None
            if has_po:
                po = PurchaseOrder(
                    po_number=po_number,
                    vendor_id=None if case == "unknown_vendor" else po_vendor.id,
                    currency="USD",
                    subtotal=Decimal("100.00"),
                    tax_amount=Decimal("0.00"),
                    total_amount=Decimal("100.00"),
                    is_current=True,
                )
                db.add(po)
                db.flush()
                po_line = PurchaseOrderLine(
                    po_id=po.id,
                    item_code="DEMO-SKU",
                    description="Synthetic demo item",
                    qty=Decimal("100"),
                    unit_price=Decimal("1.00"),
                    line_amount=Decimal("100.00"),
                )
                db.add(po_line)
                db.flush()
            quantity = (
                Decimal("100") if case == "qty_100_received_80" else Decimal("10")
            )
            received = Decimal("80") if case == "qty_100_received_80" else quantity
            if po and po_line and case != "no_gr":
                receipt = GoodsReceipt(
                    gr_number=f"DEMO-GR-{index:02d}",
                    po_id=po.id,
                    vendor_id=po.vendor_id or vendor.id,
                    receipt_date=date.today(),
                )
                db.add(receipt)
                db.flush()
                db.add(
                    GoodsReceiptLine(
                        goods_receipt_id=receipt.id,
                        po_line_id=po_line.id,
                        item_code="DEMO-SKU",
                        description="Synthetic demo item",
                        received_qty=received,
                        accepted_qty=received,
                        rejected_qty=Decimal("0"),
                    )
                )
            unit_price = Decimal("1.05") if case == "price_5pct" else Decimal("1.00")
            line_code = "NOT-IN-PO" if case == "line_not_on_po" else "DEMO-SKU"
            tax = Decimal("5") if case == "tax_mismatch" else Decimal("0")
            subtotal = quantity * unit_price
            total = (
                subtotal
                + tax
                + (Decimal("1") if case == "header_total_mismatch" else 0)
            )
            confidence = {key: dict(value) for key, value in CONFIDENCE.items()}
            if case == "low_confidence":
                confidence["total"] = {"confidence": 84, "derived": False}
            if case == "multiple_discrepancies":
                unit_price = Decimal("1.05")
                quantity = Decimal("100")
                subtotal = quantity * unit_price
                total = subtotal + 2
                tax = Decimal("2")
            db.add(
                Invoice(
                    invoice_no=invoice_no,
                    vendor_id=None if case == "unknown_vendor" else vendor.id,
                    vendor_name_raw=(
                        "Unknown Synthetic Vendor"
                        if case == "unknown_vendor"
                        else vendor.name
                    ),
                    po_number_raw=po_number if has_po else "NO-SUCH-PO",
                    po_id=po.id if po else None,
                    currency="EUR" if case == "currency_mismatch" else "USD",
                    subtotal=subtotal,
                    tax_amount=tax,
                    total_amount=total,
                    invoice_date=date.today(),
                    field_confidence=confidence,
                    status="processing",
                    is_current=True,
                )
            )
            db.flush()
            invoice = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).one()
            db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    item_code=line_code,
                    description="Synthetic demo item",
                    qty=quantity,
                    unit_price=unit_price,
                    line_amount=quantity * unit_price,
                    field_confidence={
                        key: {"confidence": 95, "derived": False}
                        for key in ("qty", "unit_price", "line_amount")
                    },
                )
            )
            db.flush()
            # Persist the same deterministic decision the API and worker use so
            # each seeded example is immediately visible in the demo UI.
            from src.services.invoice_matching_services import run_match_for_invoice

            run_match_for_invoice(db, invoice.id, force=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    seed()
