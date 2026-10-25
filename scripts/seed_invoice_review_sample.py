"""Idempotently seed the PO and receipt used in the invoice review walkthrough."""

from datetime import date
from decimal import Decimal

from src.db.session_db import SessionLocal
from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor

VENDOR_NAME = "GenWorx Hydraulic Supplies"
VENDOR_TAX_ID = "GSTIN-DEMO-2026-0001"
PO_NUMBER = "PO-2026-0001"
GR_NUMBER = "GR-2026-0001"
ITEM_CODE = "HYD-SEAL-KIT"
DESCRIPTION = "Hydraulic seal kit"
QUANTITY = Decimal("65")
UNIT_PRICE = Decimal("6455.01")
LINE_TOTAL = QUANTITY * UNIT_PRICE


def seed() -> None:
    with SessionLocal.begin() as db:
        vendor = db.query(Vendor).filter(Vendor.tax_id == VENDOR_TAX_ID).first()
        if vendor is None:
            vendor = Vendor(name=VENDOR_NAME, tax_id=VENDOR_TAX_ID, is_active=True)
            db.add(vendor)
            db.flush()

        po = (
            db.query(PurchaseOrder).filter(PurchaseOrder.po_number == PO_NUMBER).first()
        )
        if po is None:
            po = PurchaseOrder(
                po_number=PO_NUMBER,
                vendor_id=vendor.id,
                currency="USD",
                subtotal=LINE_TOTAL,
                tax_amount=Decimal("0"),
                total_amount=LINE_TOTAL,
                is_current=True,
            )
            db.add(po)
            db.flush()
        elif po.vendor_id != vendor.id:
            raise ValueError(f"{PO_NUMBER} already belongs to a different vendor")

        po_line = (
            db.query(PurchaseOrderLine)
            .filter(
                PurchaseOrderLine.po_id == po.id,
                PurchaseOrderLine.item_code == ITEM_CODE,
            )
            .first()
        )
        if po_line is None:
            po_line = PurchaseOrderLine(
                po_id=po.id,
                item_code=ITEM_CODE,
                description=DESCRIPTION,
                uom="EA",
                qty=QUANTITY,
                unit_price=UNIT_PRICE,
                line_amount=LINE_TOTAL,
            )
            db.add(po_line)
            db.flush()

        receipt = (
            db.query(GoodsReceipt).filter(GoodsReceipt.gr_number == GR_NUMBER).first()
        )
        if receipt is None:
            receipt = GoodsReceipt(
                gr_number=GR_NUMBER,
                po_id=po.id,
                vendor_id=vendor.id,
                receipt_date=date.today(),
            )
            db.add(receipt)
            db.flush()
        elif receipt.po_id != po.id or receipt.vendor_id != vendor.id:
            raise ValueError(f"{GR_NUMBER} already references a different PO or vendor")
        receipt_line = (
            db.query(GoodsReceiptLine)
            .filter(
                GoodsReceiptLine.goods_receipt_id == receipt.id,
                GoodsReceiptLine.po_line_id == po_line.id,
            )
            .first()
        )
        if receipt_line is None:
            db.add(
                GoodsReceiptLine(
                    goods_receipt_id=receipt.id,
                    po_line_id=po_line.id,
                    item_code=ITEM_CODE,
                    description=DESCRIPTION,
                    uom="EA",
                    received_qty=QUANTITY,
                    accepted_qty=QUANTITY,
                    rejected_qty=Decimal("0"),
                )
            )
    print(
        f"Seeded {PO_NUMBER} and {GR_NUMBER}: {DESCRIPTION}, qty 65, "
        "unit price 6455.01 USD"
    )


if __name__ == "__main__":
    seed()
