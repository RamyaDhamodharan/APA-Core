"""Persist successfully extracted purchase orders and goods receipts for review."""

from alembic import op

revision = "20261024_0023"
down_revision = "20261023_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.purchase_orders ADD COLUMN IF NOT EXISTS "
        "field_confidence jsonb"
    )
    op.execute(
        "ALTER TABLE core.purchase_orders ADD COLUMN IF NOT EXISTS "
        "needs_verification boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE core.po_lines ADD COLUMN IF NOT EXISTS field_confidence jsonb"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_order_source_document "
        "ON core.purchase_orders (source_document_id) "
        "WHERE source_document_id IS NOT NULL"
    )
    op.execute("ALTER TABLE core.purchase_orders ALTER COLUMN currency DROP NOT NULL")
    op.execute(
        "ALTER TABLE core.goods_receipts ADD COLUMN IF NOT EXISTS "
        "field_confidence jsonb"
    )
    op.execute(
        "ALTER TABLE core.goods_receipts ADD COLUMN IF NOT EXISTS "
        "needs_verification boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ADD COLUMN IF NOT EXISTS "
        "field_confidence jsonb"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goods_receipts_source_document "
        "ON core.goods_receipts (source_document_id) "
        "WHERE source_document_id IS NOT NULL"
    )
    op.execute("ALTER TABLE core.goods_receipts ALTER COLUMN po_id DROP NOT NULL")
    op.execute("ALTER TABLE core.goods_receipts ALTER COLUMN vendor_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE core.goods_receipts ALTER COLUMN receipt_date DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN po_line_id DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN received_qty DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN accepted_qty DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN rejected_qty DROP NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN rejected_qty SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN accepted_qty SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN received_qty SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.goods_receipt_lines ALTER COLUMN po_line_id SET NOT NULL"
    )
    op.execute("ALTER TABLE core.goods_receipts ALTER COLUMN receipt_date SET NOT NULL")
    op.execute("ALTER TABLE core.goods_receipts ALTER COLUMN vendor_id SET NOT NULL")
    op.execute("ALTER TABLE core.goods_receipts ALTER COLUMN po_id SET NOT NULL")
    op.execute("ALTER TABLE core.purchase_orders ALTER COLUMN currency SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS core.uq_goods_receipts_source_document")
    op.execute("DROP INDEX IF EXISTS core.uq_purchase_order_source_document")
    op.execute(
        "ALTER TABLE core.goods_receipts DROP COLUMN IF EXISTS needs_verification"
    )
    op.execute("ALTER TABLE core.goods_receipts DROP COLUMN IF EXISTS field_confidence")
    op.execute(
        "ALTER TABLE core.purchase_orders DROP COLUMN IF EXISTS needs_verification"
    )
    op.execute(
        "ALTER TABLE core.purchase_orders DROP COLUMN IF EXISTS field_confidence"
    )
