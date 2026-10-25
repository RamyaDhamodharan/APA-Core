"""Separate document data review from processing failures."""

from alembic import op

revision = "20261023_0020"
down_revision = "20261023_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires a commit between adding an enum value and using it.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE core.doc_status ADD VALUE IF NOT EXISTS 'needs_data_review'"
        )
    op.execute(
        "UPDATE core.documents SET status = 'needs_data_review' "
        "WHERE status = 'needs_vendor_review'"
    )
    op.execute(
        "UPDATE core.documents SET failure_message = "
        "'Review the extracted fields and correct missing or mismatched values.' "
        "WHERE status = 'needs_data_review' AND failure_message = "
        "'This file could not be processed; check the file and retry.'"
    )
    op.execute(
        "UPDATE core.documents AS document SET failure_message = "
        "'Document extraction service failed while reading this file; retry "
        "processing. If it fails again, contact support.' "
        "WHERE document.status = 'failed' AND document.failure_message = "
        "'This file could not be processed; check the file and retry.' "
        "AND EXISTS (SELECT 1 FROM core.audit_log AS audit "
        "WHERE audit.entity_type = 'document' "
        "AND audit.entity_id = document.id AND audit.step_name = 'parse' "
        "AND audit.result = 'error' "
        "AND audit.output_summary->>'error_type' = 'ConversionError')"
    )
    op.execute(
        "UPDATE core.documents SET failure_message = "
        "'This email could not be processed automatically. Retry to recover its "
        "supported attachments.' "
        "WHERE status = 'failed' AND (original_filename ILIKE '%.eml' OR mime = "
        "'message/rfc822') AND failure_message ILIKE "
        "'%stopped unexpectedly after 60 minutes%'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE core.documents SET status = 'needs_vendor_review' "
        "WHERE status = 'needs_data_review'"
    )
