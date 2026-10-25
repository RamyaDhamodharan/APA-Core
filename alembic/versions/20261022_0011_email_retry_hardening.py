"""Persist retry scheduling and allow ignored email rows without raw objects."""

from alembic import op

revision = "20261022_0011"
down_revision = "20261022_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.email_messages "
        "ALTER COLUMN raw_object_key DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE core.email_messages ADD COLUMN IF NOT EXISTS "
        "next_retry_at timestamptz"
    )
    op.execute(
        "ALTER TABLE core.email_messages ADD COLUMN IF NOT EXISTS "
        "failure_reason varchar(200)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_messages_due_retry "
        "ON core.email_messages(mailbox, folder, next_retry_at) "
        "WHERE status = 'failed'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.ix_email_messages_due_retry")
    op.execute(
        "ALTER TABLE core.email_messages DROP COLUMN IF EXISTS next_retry_at"
    )
    op.execute(
        "ALTER TABLE core.email_messages DROP COLUMN IF EXISTS failure_reason"
    )
    # Older placeholder retry rows used a synthetic key, so restore NOT NULL safely.
    op.execute(
        "UPDATE core.email_messages SET raw_object_key = "
        "'emails/pending/' || id::text || '.eml' WHERE raw_object_key IS NULL"
    )
    op.execute(
        "ALTER TABLE core.email_messages ALTER COLUMN raw_object_key SET NOT NULL"
    )
