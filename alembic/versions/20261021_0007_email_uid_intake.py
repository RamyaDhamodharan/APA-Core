"""Add durable UID checkpoint and email intake records."""

from alembic import op

revision = "20261021_0007"
down_revision = "20261021_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE doc_status ADD VALUE IF NOT EXISTS 'other'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_checkpoints (
          mailbox varchar(300) PRIMARY KEY,
          uidvalidity integer NOT NULL,
          last_uid integer NOT NULL DEFAULT 0,
          updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_messages (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          message_id varchar(500) NOT NULL,
          uid integer NOT NULL,
          mailbox varchar(300) NOT NULL,
          sender varchar(300),
          subject varchar(500),
          received_at timestamptz,
          body_preview text,
          raw_object_key varchar(500) NOT NULL,
          status varchar(40) NOT NULL DEFAULT 'received',
          attempts integer NOT NULL DEFAULT 0,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_email_messages_message_id UNIQUE(message_id)
        )
    """)
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
        "classify_confidence double precision"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS classify_override varchar(30)"
    )
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS sha256 char(64)")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime varchar(120)")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS size integer")


def downgrade() -> None:
    op.execute("ALTER TABLE documents ALTER COLUMN status DROP DEFAULT")
    op.execute("UPDATE documents SET status = 'received' WHERE status = 'other'")
    op.execute("""
        CREATE TYPE doc_status_without_other AS ENUM (
          'received', 'parsing', 'parsed', 'classified', 'extracted',
          'needs_vendor_review', 'processed', 'ignored', 'incomplete',
          'duplicate_blocked', 'failed'
        )
    """)
    op.execute(
        "ALTER TABLE documents ALTER COLUMN status TYPE doc_status_without_other "
        "USING status::text::doc_status_without_other"
    )
    op.execute("DROP TYPE doc_status")
    op.execute("ALTER TYPE doc_status_without_other RENAME TO doc_status")
    op.execute(
        "ALTER TABLE documents ALTER COLUMN status SET DEFAULT 'received'::doc_status"
    )
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS size")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS mime")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS sha256")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS classify_confidence")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS classify_override")
    op.execute("DROP TABLE IF EXISTS email_messages")
    op.execute("DROP TABLE IF EXISTS email_checkpoints")
