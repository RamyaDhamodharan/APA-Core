"""Persist bounded document retry state and one-time bulk processing marker."""

from alembic import op

revision = "20261025_0024"
down_revision = "20261024_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS failure_class varchar(24)"
    )
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS retry_count integer "
        "NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS "
        "next_retry_at timestamptz"
    )
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS "
        "last_attempt_at timestamptz"
    )
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS "
        "bulk_reprocessed_at timestamptz"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_retry_due "
        "ON core.documents (next_retry_at) WHERE failure_class = 'retryable'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.ix_documents_retry_due")
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS bulk_reprocessed_at")
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS last_attempt_at")
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS next_retry_at")
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS retry_count")
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS failure_class")
