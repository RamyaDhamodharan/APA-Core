"""human reviewed outbound email drafts and reply events

Revision ID: 20261022_0014
Revises: 20261022_0013
"""

from alembic import op

revision = "20261022_0014"
down_revision = "20261022_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS core.email_drafts (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          kind varchar(50) NOT NULL,
          invoice_id uuid REFERENCES core.invoices(id) ON DELETE CASCADE,
          issue_id uuid,
          document_id uuid REFERENCES core.documents(id),
          to_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
          cc_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
          subject varchar(500) NOT NULL,
          body text NOT NULL,
          status varchar(20) NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','approved','sent','failed','discarded')),
          blocked_by_allowlist boolean NOT NULL DEFAULT false,
          created_by uuid REFERENCES auth.users(id),
          generated_by varchar(20) NOT NULL DEFAULT 'template',
          edited_by uuid REFERENCES auth.users(id),
          sent_by uuid REFERENCES auth.users(id),
          sent_at timestamptz,
          message_id varchar(500),
          thread_key varchar(100),
          last_error varchar(500),
          attempts integer NOT NULL DEFAULT 0,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        """CREATE INDEX IF NOT EXISTS ix_email_drafts_invoice_created
        ON core.email_drafts(invoice_id, created_at)"""
    )
    op.execute(
        """CREATE INDEX IF NOT EXISTS ix_email_drafts_thread_key
        ON core.email_drafts(thread_key)"""
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS core.email_draft_events (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          draft_id uuid NOT NULL REFERENCES core.email_drafts(id) ON DELETE CASCADE,
          event_type varchar(30) NOT NULL CHECK (event_type IN (
            'created','edited','sent','failed','discarded','vendor_replied')),
          user_id uuid REFERENCES auth.users(id),
          old_value jsonb,
          new_value jsonb,
          document_id uuid REFERENCES core.documents(id),
          message_id varchar(500),
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        """CREATE INDEX IF NOT EXISTS ix_email_draft_events_draft_created
        ON core.email_draft_events(draft_id, created_at)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.email_draft_events")
    op.execute("DROP TABLE IF EXISTS core.email_drafts")
