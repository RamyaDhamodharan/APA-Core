"""Add invoice workspace status, match candidates and discrepancies."""

from alembic import op

revision = "20261023_0018"
down_revision = "20261022_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE core.audit_log ADD COLUMN IF NOT EXISTS actor varchar(120)")
    op.execute(
        "DO $$ BEGIN CREATE TYPE core.invoice_status AS ENUM "
        "('processing','needs_review','ready_to_pay','on_hold','rejected'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS status "
        "core.invoice_status NOT NULL DEFAULT 'processing'"
    )
    op.execute(
        "UPDATE core.invoices SET status = CASE "
        "WHEN final_status = 'rejected' THEN 'rejected'::core.invoice_status "
        "WHEN final_status = 'on_hold' THEN 'on_hold'::core.invoice_status "
        "WHEN final_status = 'approved' OR match_status::text = 'ready_for_review' "
        "THEN 'ready_to_pay'::core.invoice_status "
        "WHEN workflow_status = 'failed' THEN 'processing'::core.invoice_status "
        "WHEN match_status::text IN ('match_issues','no_match','needs_vendor_review') "
        "THEN 'needs_review'::core.invoice_status "
        "WHEN workflow_status IN ('match_issues','no_match') "
        "THEN 'needs_review'::core.invoice_status "
        "ELSE 'processing'::core.invoice_status END"
    )
    op.execute("ALTER TABLE core.invoices ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE core.invoices ALTER COLUMN status SET DEFAULT 'processing'")

    # Remove the obsolete value from the legacy matching enum after migrating rows.
    op.execute("ALTER TABLE core.invoices ALTER COLUMN match_status DROP DEFAULT")
    op.execute("ALTER TYPE core.match_status RENAME TO match_status_legacy")
    op.execute(
        "CREATE TYPE core.match_status AS ENUM "
        "('pending','needs_vendor_review','waiting_for_po','needs_po_confirmation',"
        "'match_issues','no_match')"
    )
    op.execute(
        "ALTER TABLE core.invoices ALTER COLUMN match_status TYPE core.match_status "
        "USING (CASE WHEN match_status::text = 'ready_for_review' THEN 'pending' "
        "ELSE match_status::text END)::core.match_status"
    )
    op.execute(
        "ALTER TABLE core.invoices ALTER COLUMN match_status SET DEFAULT 'pending'"
    )
    op.execute("DROP TYPE core.match_status_legacy")

    op.execute(
        "CREATE TABLE core.invoice_matches ("
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE, "
        "po_id uuid REFERENCES core.purchase_orders(id), "
        "gr_id uuid REFERENCES core.goods_receipts(id), "
        "match_type varchar(10) NOT NULL, score numeric(5,2), "
        "status varchar(20) NOT NULL DEFAULT 'suggested', "
        "confirmed_by uuid REFERENCES auth.users(id), "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now(), "
        "CONSTRAINT ck_invoice_matches_type CHECK (match_type IN ('2-way','3-way')), "
        "CONSTRAINT ck_invoice_matches_status CHECK "
        "(status IN ('suggested','confirmed','rejected'))"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_invoice_matches_invoice "
        "ON core.invoice_matches(invoice_id,status)"
    )
    op.execute(
        "CREATE TABLE core.invoice_discrepancies ("
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE, "
        "field varchar(80) NOT NULL, invoice_value jsonb, po_value jsonb, "
        "gr_value jsonb, "
        "severity varchar(20) NOT NULL DEFAULT 'warning', "
        "resolved boolean NOT NULL DEFAULT false, "
        "reason text, created_at timestamptz NOT NULL DEFAULT now())"
    )
    op.execute(
        "CREATE INDEX ix_invoice_discrepancies_invoice "
        "ON core.invoice_discrepancies(invoice_id,resolved)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.invoice_discrepancies")
    op.execute("DROP TABLE IF EXISTS core.invoice_matches")
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE core.invoices ALTER COLUMN match_status DROP DEFAULT")
    op.execute("ALTER TYPE core.match_status RENAME TO match_status_current")
    op.execute(
        "CREATE TYPE core.match_status AS ENUM "
        "('pending','needs_vendor_review','waiting_for_po','needs_po_confirmation',"
        "'ready_for_review','match_issues','no_match')"
    )
    op.execute(
        "ALTER TABLE core.invoices ALTER COLUMN match_status TYPE core.match_status "
        "USING (match_status::text)::core.match_status"
    )
    op.execute(
        "ALTER TABLE core.invoices ALTER COLUMN match_status SET DEFAULT 'pending'"
    )
    op.execute("DROP TYPE core.match_status_current")
    op.execute("DROP TYPE core.invoice_status")
