"""Use folder-scoped UID polling state and retain email metadata on documents."""

from alembic import op

revision = "20261022_0008"
down_revision = "20261021_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_poll_state (
          mailbox varchar(300) NOT NULL,
          folder varchar(300) NOT NULL,
          uidvalidity integer NOT NULL,
          last_uid integer NOT NULL DEFAULT 0,
          last_run_at timestamptz,
          last_result jsonb,
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT pk_email_poll_state PRIMARY KEY (mailbox, folder)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('email_checkpoints') IS NOT NULL THEN
            INSERT INTO email_poll_state
              (mailbox, folder, uidvalidity, last_uid, updated_at)
            SELECT mailbox, 'INBOX', uidvalidity, last_uid, updated_at
            FROM email_checkpoints
            ON CONFLICT (mailbox, folder) DO NOTHING;
          END IF;
        END;
        $$
        """
    )
    op.execute(
        "ALTER TABLE email_messages ADD COLUMN IF NOT EXISTS "
        "folder varchar(300) NOT NULL DEFAULT 'INBOX'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_folder_uid "
        "ON email_messages(mailbox, folder, uid)"
    )
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_uid integer")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_body_preview text")
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_eml_key varchar(500)"
    )
    op.execute("DROP TABLE IF EXISTS email_checkpoints")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_checkpoints (
          mailbox varchar(300) PRIMARY KEY,
          uidvalidity integer NOT NULL,
          last_uid integer NOT NULL DEFAULT 0,
          updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO email_checkpoints (mailbox, uidvalidity, last_uid, updated_at)
        SELECT mailbox, uidvalidity, last_uid, updated_at
        FROM email_poll_state
        WHERE folder = 'INBOX'
        ON CONFLICT (mailbox) DO UPDATE SET
          uidvalidity = EXCLUDED.uidvalidity,
          last_uid = EXCLUDED.last_uid,
          updated_at = EXCLUDED.updated_at
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_email_messages_mailbox_folder_uid")
    op.execute("ALTER TABLE email_messages DROP COLUMN IF EXISTS folder")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS email_eml_key")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS email_body_preview")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS email_uid")
    op.execute("DROP TABLE IF EXISTS email_poll_state")
