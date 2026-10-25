"""Store optional PO payment and delivery terms.

Revision ID: 20261021_0005
Revises: 20261020_0004
"""

from alembic import op

revision = "20261021_0005"
down_revision = "20261020_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS payment_terms jsonb"
    )
    op.execute(
        "ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS delivery_details jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE purchase_orders DROP COLUMN IF EXISTS delivery_details")
    op.execute("ALTER TABLE purchase_orders DROP COLUMN IF EXISTS payment_terms")
