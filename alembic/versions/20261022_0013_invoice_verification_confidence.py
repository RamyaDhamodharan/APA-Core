"""invoice field confidence, GL and tax expectations

Revision ID: 20261022_0013
Revises: 20261022_0012
"""

from alembic import op

revision = "20261022_0013"
down_revision = "20261022_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS "
        "field_confidence jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS gl_code varchar(60)")
    op.execute(
        "ALTER TABLE core.invoice_lines ADD COLUMN IF NOT EXISTS "
        "field_confidence jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE core.purchase_orders ADD COLUMN IF NOT EXISTS "
        "tax_rate numeric(7,4)"
    )
    op.execute(
        "ALTER TABLE core.vendors ADD COLUMN IF NOT EXISTS "
        "default_tax_rate numeric(7,4)"
    )
    op.execute(
        "UPDATE core.invoices SET field_confidence = jsonb_build_object("
        "'vendor', jsonb_build_object('confidence', CASE WHEN vendor_id IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), "
        "'invoice_number', jsonb_build_object('confidence', CASE WHEN invoice_no "
        "IS NULL THEN 0 ELSE 75 END, 'derived', true), "
        "'invoice_date', jsonb_build_object('confidence', CASE WHEN invoice_date "
        "IS NULL THEN 0 ELSE 75 END, 'derived', true), "
        "'due_date', jsonb_build_object('confidence', CASE WHEN due_date IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), "
        "'currency', jsonb_build_object('confidence', CASE WHEN currency IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), "
        "'subtotal', jsonb_build_object('confidence', CASE WHEN subtotal IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), "
        "'tax', jsonb_build_object('confidence', CASE WHEN tax_amount IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), "
        "'total', jsonb_build_object('confidence', CASE WHEN total_amount IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), "
        "'order_number', jsonb_build_object('confidence', CASE WHEN po_number_raw "
        "IS NULL THEN 0 ELSE 75 END, 'derived', true), "
        "'gl_code', jsonb_build_object('confidence', CASE WHEN gl_code IS NULL "
        "THEN 0 ELSE 75 END, 'derived', true), 'line_items', '[]'::jsonb) "
        "WHERE field_confidence = '{}'::jsonb"
    )
    op.execute(
        "UPDATE core.invoice_lines SET field_confidence = jsonb_build_object("
        "'qty', jsonb_build_object('confidence', CASE WHEN qty IS NULL THEN 0 "
        "ELSE 75 END, 'derived', true), "
        "'unit_price', jsonb_build_object('confidence', CASE WHEN unit_price IS "
        "NULL THEN 0 ELSE 75 END, 'derived', true), "
        "'line_amount', jsonb_build_object('confidence', CASE WHEN line_amount IS "
        "NULL THEN 0 ELSE 75 END, 'derived', true)) "
        "WHERE field_confidence = '{}'::jsonb"
    )
    op.execute(
        "INSERT INTO core.invoice_state_history "
        "(invoice_id, from_status, to_status, reason) "
        "SELECT id, workflow_status::text, 'verifying', "
        "'Existing invoice fields require associate verification' "
        "FROM core.invoices WHERE workflow_status NOT IN "
        "('approved','rejected','on_hold','paid','overdue','verifying','no_match') "
        "AND final_status IS NULL"
    )
    op.execute(
        "UPDATE core.invoices SET workflow_status = 'verifying', "
        "match_status = 'needs_vendor_review' WHERE workflow_status NOT IN "
        "('approved','rejected','on_hold','paid','overdue','verifying','no_match') "
        "AND final_status IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invoices_gl_code ON core.invoices(gl_code)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE core.invoices AS invoice SET "
        "workflow_status = history.from_status::core.invoice_workflow_status, "
        "match_status = CASE history.from_status "
        "WHEN 'verifying' THEN 'needs_vendor_review'::core.match_status "
        "WHEN 'matching' THEN 'waiting_for_po'::core.match_status "
        "WHEN 'match_approved' THEN 'pending'::core.match_status "
        "WHEN 'match_issues' THEN 'match_issues'::core.match_status "
        "WHEN 'no_match' THEN 'no_match'::core.match_status "
        "ELSE 'pending'::core.match_status END "
        "FROM core.invoice_state_history AS history "
        "WHERE history.invoice_id = invoice.id "
        "AND history.reason = 'Existing invoice fields require associate verification' "
        "AND invoice.workflow_status = 'verifying' "
        "AND invoice.match_status = 'needs_vendor_review'"
    )
    op.execute(
        "DELETE FROM core.invoice_state_history "
        "WHERE reason = 'Existing invoice fields require associate verification'"
    )
    op.execute("DROP INDEX IF EXISTS core.ix_invoices_gl_code")
    op.execute("ALTER TABLE core.vendors DROP COLUMN IF EXISTS default_tax_rate")
    op.execute("ALTER TABLE core.purchase_orders DROP COLUMN IF EXISTS tax_rate")
    op.execute("ALTER TABLE core.invoice_lines DROP COLUMN IF EXISTS field_confidence")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS gl_code")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS field_confidence")
