"""payments and scheduled reminders

Revision ID: 20261022_0016
Revises: 20261022_0015
"""

from alembic import op

revision = "20261022_0016"
down_revision = "20261022_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE IF NOT EXISTS core.payments (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
        amount numeric(14,2) NOT NULL CHECK (amount >= 0),
        paid_date date NOT NULL,
        method varchar(20) NOT NULL CHECK (method IN
          ('bank_transfer','cheque','upi','cash','other')),
        reference varchar(200) NOT NULL,
        recorded_by uuid NOT NULL REFERENCES auth.users(id),
        note text,
        created_at timestamptz NOT NULL DEFAULT now(),
        voided_at timestamptz,
        voided_by uuid REFERENCES auth.users(id),
        void_reason text
      )
    """)
    op.execute("""
      CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_active_invoice
      ON core.payments(invoice_id) WHERE voided_at IS NULL
    """)
    op.execute("""
      CREATE TABLE IF NOT EXISTS core.follow_ups (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        entity_type varchar(20) NOT NULL CHECK (entity_type IN ('invoice','issue')),
        entity_id uuid NOT NULL,
        kind varchar(40) NOT NULL CHECK (kind IN (
          'verify_pending','approval_pending','issue_unassigned','issue_waiting_vendor',
          'overdue','rejected_not_resubmitted','due_soon','on_hold_stale')),
        recipient varchar(200),
        attempts integer NOT NULL DEFAULT 0,
        last_sent_at timestamptz,
        next_due_at timestamptz NOT NULL,
        stopped_at timestamptz,
        stop_reason text,
        created_at timestamptz NOT NULL DEFAULT now()
      )
    """)
    op.execute("""
      CREATE UNIQUE INDEX IF NOT EXISTS uq_followups_active_entity_kind
      ON core.follow_ups(entity_type, entity_id, kind) WHERE stopped_at IS NULL
    """)
    op.execute("""
      CREATE INDEX IF NOT EXISTS ix_follow_ups_due
      ON core.follow_ups(stopped_at, next_due_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.follow_ups")
    op.execute("DROP TABLE IF EXISTS core.payments")
