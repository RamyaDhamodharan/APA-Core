"""persist email filter reasons and sender classification hints

Revision ID: 20261022_0017
Revises: 20261022_0016
"""

from alembic import op

revision = "20261022_0017"
down_revision = "20261022_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.email_messages ADD COLUMN IF NOT EXISTS "
        "filter_reason varchar(200)"
    )
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS source_flag varchar(100)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS source_flag")
    op.execute("ALTER TABLE core.email_messages DROP COLUMN IF EXISTS filter_reason")
