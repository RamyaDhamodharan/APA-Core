"""Add goods receipts and line quantities to the shared AP schema.

Revision ID: 20261020_0003
Revises: 0f7ca912d0e0
"""

from alembic import op

revision = "20261020_0003"
down_revision = "0f7ca912d0e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extend the shared PostgreSQL enum. IF NOT EXISTS keeps this safe when a
    # database has already been updated from the shared bootstrap contract.
    op.execute("ALTER TYPE doc_type ADD VALUE IF NOT EXISTS 'goods_receipt'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS goods_receipts (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          gr_number varchar(60) NOT NULL,
          po_id uuid NOT NULL REFERENCES purchase_orders(id),
          vendor_id uuid NOT NULL REFERENCES vendors(id),
          source_document_id uuid REFERENCES documents(id),
          receipt_date date NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          created_by uuid REFERENCES users(id),
          updated_at timestamptz NOT NULL DEFAULT now(),
          updated_by uuid REFERENCES users(id),
          CONSTRAINT uq_goods_receipts_gr_number UNIQUE (gr_number)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_goods_receipts_po_id ON goods_receipts(po_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goods_receipts_gr_number_norm "
        "ON goods_receipts(lower(trim(gr_number)))"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS goods_receipt_lines (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          goods_receipt_id uuid NOT NULL REFERENCES goods_receipts(id)
            ON DELETE CASCADE,
          po_line_id uuid NOT NULL REFERENCES po_lines(id),
          item_code varchar(60),
          description varchar(300),
          uom varchar(10),
          received_qty numeric(14,3) NOT NULL CHECK (received_qty > 0),
          accepted_qty numeric(14,3) NOT NULL CHECK (accepted_qty >= 0),
          rejected_qty numeric(14,3) NOT NULL DEFAULT 0 CHECK (rejected_qty >= 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          created_by uuid REFERENCES users(id),
          updated_at timestamptz NOT NULL DEFAULT now(),
          updated_by uuid REFERENCES users(id),
          CONSTRAINT ck_gr_line_qty_balance CHECK (
            accepted_qty + rejected_qty = received_qty
          )
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS goods_receipt_lines")
    op.execute("DROP INDEX IF EXISTS uq_goods_receipts_gr_number_norm")
    op.execute("DROP INDEX IF EXISTS ix_goods_receipts_po_id")
    op.execute("DROP TABLE IF EXISTS goods_receipts")
    # PostgreSQL enum values cannot be removed safely; keep the unused label.
