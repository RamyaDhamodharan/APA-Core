"""Persist UIDVALIDITY with each mailbox message identity."""

from alembic import op

revision = "20261023_0021"
down_revision = "20261023_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.email_messages ADD COLUMN IF NOT EXISTS "
        "uidvalidity integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "UPDATE core.email_messages AS message SET uidvalidity = state.uidvalidity "
        "FROM core.email_poll_state AS state "
        "WHERE message.mailbox = state.mailbox AND message.folder = state.folder "
        "AND message.uidvalidity = 0"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_folder_validity_uid "
        "ON core.email_messages(mailbox, folder, uidvalidity, uid)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS core.ix_email_messages_mailbox_folder_validity_uid"
    )
    op.execute("ALTER TABLE core.email_messages DROP COLUMN IF EXISTS uidvalidity")
