"""Add email ingestion metadata and idempotency key."""

from alembic import op

revision = "20261021_0006"
down_revision = "20261021_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_message_id varchar(300)"
    )
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source varchar(40)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_email_message_id_root "
        "ON documents(email_message_id) WHERE parent_document_id IS NULL "
        "AND email_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_documents_email_message_id_root")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS source")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS email_message_id")
