"""Add normalized invoice number keys for duplicate detection."""

from sqlalchemy import text

from alembic import op
from src.services.invoice_number_services import normalize_invoice_no

revision = "20261023_0022"
down_revision = "20261023_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS "
        "invoice_no_normalized varchar(180)"
    )
    connection = op.get_bind()
    rows = connection.execute(
        text("SELECT id, invoice_no FROM core.invoices WHERE invoice_no IS NOT NULL")
    )
    for invoice_id, invoice_no in rows:
        connection.execute(
            text(
                "UPDATE core.invoices SET invoice_no_normalized = :normalized "
                "WHERE id = :invoice_id"
            ),
            {
                "invoice_id": invoice_id,
                "normalized": normalize_invoice_no(invoice_no),
            },
        )
    duplicates = connection.execute(
        text(
            "SELECT vendor_id FROM core.invoices "
            "WHERE is_current AND vendor_id IS NOT NULL "
            "AND invoice_no_normalized IS NOT NULL "
            "GROUP BY vendor_id, invoice_no_normalized HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates:
        raise RuntimeError(
            "Cannot add normalized invoice duplicate index: existing current "
            "invoice numbers collide after normalization. Resolve those rows and retry."
        )
    op.execute("DROP INDEX IF EXISTS core.uq_current_vendor_invoice")
    op.execute(
        "CREATE UNIQUE INDEX uq_current_vendor_invoice_normalized "
        "ON core.invoices(vendor_id, invoice_no_normalized) "
        "WHERE is_current AND vendor_id IS NOT NULL "
        "AND invoice_no_normalized IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.uq_current_vendor_invoice_normalized")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS invoice_no_normalized")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_current_vendor_invoice "
        "ON core.invoices(vendor_id, invoice_no) "
        "WHERE is_current AND vendor_id IS NOT NULL AND invoice_no IS NOT NULL"
    )
