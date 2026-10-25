"""Normalize email source labels and backfill missing email failure reasons."""

from alembic import op

revision = "20261023_0019"
down_revision = "20261023_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE core.email_messages "
        "SET failure_reason = 'Unknown (before reason tracking)' "
        "WHERE status IN ('failed', 'failed_final') "
        "AND (failure_reason IS NULL OR btrim(failure_reason) = '')"
    )
    op.execute(
        "UPDATE core.email_messages "
        "SET filter_reason = 'Unknown (before reason tracking)' "
        "WHERE status = 'ignored' "
        "AND (filter_reason IS NULL OR btrim(filter_reason) = '')"
    )
    op.execute(
        "UPDATE core.documents SET source = 'email' WHERE source = 'email_poll'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE core.documents SET source = 'email_poll' WHERE source = 'email'"
    )
