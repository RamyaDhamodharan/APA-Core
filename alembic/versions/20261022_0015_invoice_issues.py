"""invoice issue tracking and resolution

Revision ID: 20261022_0015
Revises: 20261022_0014
"""

from alembic import op

revision = "20261022_0015"
down_revision = "20261022_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE core.invoices
      ADD COLUMN IF NOT EXISTS resubmitted_from_id uuid
      REFERENCES core.invoices(id)
    """)
    op.execute("""
      CREATE TABLE IF NOT EXISTS core.invoice_issues (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
        issue_type varchar(50) NOT NULL,
        severity varchar(20) NOT NULL DEFAULT 'warning',
        description text NOT NULL,
        expected_value jsonb,
        actual_value jsonb,
        status varchar(30) NOT NULL DEFAULT 'open' CHECK (status IN (
          'open','in_progress','waiting_vendor','escalated','resolved','dismissed')),
        owner_user_id uuid REFERENCES auth.users(id), due_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
        resolution_note text
      )
    """)
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_issue_open_type
      ON core.invoice_issues(invoice_id, issue_type)
      WHERE status IN ('open','in_progress','waiting_vendor','escalated')""")
    op.execute("""
      CREATE INDEX IF NOT EXISTS ix_invoice_issues_status_due
      ON core.invoice_issues(status, due_at)
    """)
    op.execute("""
      CREATE INDEX IF NOT EXISTS ix_invoice_issues_owner_status
      ON core.invoice_issues(owner_user_id, status)
    """)
    op.execute("""
      CREATE TABLE IF NOT EXISTS core.issue_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        issue_id uuid NOT NULL REFERENCES core.invoice_issues(id) ON DELETE CASCADE,
        event_type varchar(30) NOT NULL CHECK (event_type IN (
          'created','comment','assigned','reassigned','escalated','vendor_contacted',
          'vendor_replied','status_changed','resolved','dismissed','reopened')),
        user_id uuid REFERENCES auth.users(id), note text,
        created_at timestamptz NOT NULL DEFAULT now()
      )
    """)
    op.execute("""
      DO $$ BEGIN
        IF NOT EXISTS (
          SELECT 1 FROM pg_constraint
          WHERE conname = 'fk_email_drafts_issue_id'
            AND conrelid = 'core.email_drafts'::regclass
        ) THEN
          ALTER TABLE core.email_drafts
            ADD CONSTRAINT fk_email_drafts_issue_id
            FOREIGN KEY (issue_id) REFERENCES core.invoice_issues(id) ON DELETE CASCADE;
        END IF;
      END $$
    """)
    op.execute("""
      CREATE INDEX IF NOT EXISTS ix_issue_events_timeline
      ON core.issue_events(issue_id, created_at)
    """)


def downgrade() -> None:
    op.execute("""
      ALTER TABLE core.email_drafts
      DROP CONSTRAINT IF EXISTS fk_email_drafts_issue_id
    """)
    op.execute("DROP TABLE IF EXISTS core.issue_events")
    op.execute("DROP TABLE IF EXISTS core.invoice_issues")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS resubmitted_from_id")
