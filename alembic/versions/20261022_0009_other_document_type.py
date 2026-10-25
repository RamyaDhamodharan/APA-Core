"""Allow documents outside the invoice, PO, and goods receipt categories."""

from alembic import op

revision = "20261022_0009"
down_revision = "20261022_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE doc_type ADD VALUE IF NOT EXISTS 'other'")


def downgrade() -> None:
    # PostgreSQL cannot remove an enum value without rebuilding the type.
    pass
