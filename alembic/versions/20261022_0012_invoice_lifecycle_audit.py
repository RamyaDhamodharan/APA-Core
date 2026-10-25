"""Add invoice lifecycle, state history and processing step metadata."""

from alembic import op

revision = "20261022_0012"
down_revision = "20261022_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN CREATE TYPE core.invoice_workflow_status AS ENUM "
        "('received','reading','verifying','matching','match_approved','match_issues',"
        "'no_match','approved','rejected','on_hold','paid','overdue','resubmitted',"
        "'failed'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS workflow_status "
        "core.invoice_workflow_status NOT NULL DEFAULT 'received'"
    )
    op.execute("ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS paid_at timestamptz")
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS payment_method varchar(60)"
    )
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS "
        "payment_reference varchar(200)"
    )
    op.execute(
        "UPDATE core.invoices SET workflow_status = CASE "
        "WHEN final_status IS NOT NULL THEN "
        "final_status::text::core.invoice_workflow_status "
        "WHEN match_status = 'needs_vendor_review' THEN "
        "'verifying'::core.invoice_workflow_status "
        "WHEN match_status IN ('waiting_for_po','needs_po_confirmation') THEN "
        "'matching'::core.invoice_workflow_status "
        "WHEN match_status = 'match_issues' THEN "
        "'match_issues'::core.invoice_workflow_status "
        "WHEN match_status = 'no_match' THEN 'no_match'::core.invoice_workflow_status "
        "ELSE 'received'::core.invoice_workflow_status END"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS core.invoice_state_history ("
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE, "
        "from_status varchar(30), to_status varchar(30) NOT NULL, "
        "changed_by uuid REFERENCES auth.users(id), reason text, "
        "created_at timestamptz NOT NULL DEFAULT now())"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invoice_state_history_timeline "
        "ON core.invoice_state_history(invoice_id, created_at)"
    )
    for name, definition in (
        ("reason", "text"),
        ("step_name", "varchar(60)"),
        ("duration_ms", "integer"),
        ("input_ref", "varchar(500)"),
        ("output_summary", "jsonb"),
    ):
        op.execute(
            f"ALTER TABLE core.audit_log ADD COLUMN IF NOT EXISTS {name} {definition}"
        )


def downgrade() -> None:
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS payment_reference")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS payment_method")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS paid_at")
    op.execute("ALTER TABLE core.audit_log DROP COLUMN IF EXISTS output_summary")
    op.execute("ALTER TABLE core.audit_log DROP COLUMN IF EXISTS input_ref")
    op.execute("ALTER TABLE core.audit_log DROP COLUMN IF EXISTS duration_ms")
    op.execute("ALTER TABLE core.audit_log DROP COLUMN IF EXISTS step_name")
    op.execute("ALTER TABLE core.audit_log DROP COLUMN IF EXISTS reason")
    op.execute("DROP TABLE IF EXISTS core.invoice_state_history")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS workflow_status")
    op.execute("DROP TYPE IF EXISTS core.invoice_workflow_status")
