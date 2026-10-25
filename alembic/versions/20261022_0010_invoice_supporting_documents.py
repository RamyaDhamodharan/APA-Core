"""Add auditable supporting documents attached to invoices."""

from alembic import op

revision = "20261022_0010"
down_revision = "20261022_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE core.invoice_supporting_document_category AS ENUM (
          'delivery_note', 'contract', 'approval_email', 'tax_certificate', 'other'
        )
        """
    )
    op.execute(
        """
        CREATE TABLE core.invoice_supporting_documents (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          invoice_id uuid NOT NULL REFERENCES core.invoices(id),
          file_name varchar(300) NOT NULL,
          content_type varchar(150) NOT NULL,
          size_bytes integer NOT NULL CHECK (size_bytes > 0),
          sha256 varchar(64) NOT NULL,
          storage_key varchar(700) NOT NULL,
          category core.invoice_supporting_document_category NOT NULL,
          description text,
          uploaded_by uuid NOT NULL REFERENCES auth.users(id),
          uploaded_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz,
          deleted_by uuid REFERENCES auth.users(id),
          deleted_reason text
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_invoice_supporting_documents_invoice "
        "ON core.invoice_supporting_documents(invoice_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_invoice_supporting_documents_active_sha256 "
        "ON core.invoice_supporting_documents(invoice_id, sha256) "
        "WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.invoice_supporting_documents")
    op.execute("DROP TYPE IF EXISTS core.invoice_supporting_document_category")
