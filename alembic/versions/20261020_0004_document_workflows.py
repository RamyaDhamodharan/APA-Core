"""Persist document matching workflow snapshots and human decisions.

Revision ID: 20261020_0004
Revises: 20261020_0003
"""

from alembic import op

revision = "20261020_0004"
down_revision = "20261020_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS document_workflows (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          status varchar(40) NOT NULL,
          result jsonb NOT NULL DEFAULT '{}'::jsonb,
          decision varchar(40),
          decision_target_id uuid,
          decision_target_type varchar(30),
          decision_reason varchar(500),
          decision_by uuid REFERENCES users(id),
          decided_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_document_workflows_document UNIQUE(document_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_workflows_status "
        "ON document_workflows(status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_workflows_status")
    op.execute("DROP TABLE IF EXISTS document_workflows")
