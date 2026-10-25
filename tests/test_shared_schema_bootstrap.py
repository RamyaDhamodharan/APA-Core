import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _table_definition(name: str) -> str:
    bootstrap = (ROOT / "database" / "001_initial_schema.sql").read_text()
    schema_name = "auth" if name in {"users", "refresh_sessions"} else "core"
    match = re.search(
        rf"CREATE TABLE {schema_name}\.{name}\s*\((.*?)\n\);",
        bootstrap,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_users_bootstrap_matches_ap_auth_column_and_default_contract():
    users = _table_definition("users")

    assert re.search(r"^\s*name varchar NOT NULL,", users, re.MULTILINE)
    assert re.search(r"^\s*email varchar NOT NULL UNIQUE,", users, re.MULTILINE)
    assert re.search(r"^\s*password_hash varchar NOT NULL,", users, re.MULTILINE)
    assert re.search(r"^\s*role auth\.user_role NOT NULL,", users, re.MULTILINE)
    assert "must_change_password boolean NOT NULL DEFAULT false" in users
    assert "role user_role NOT NULL DEFAULT" not in users


def test_shared_audit_columns_are_present_without_invented_foreign_keys():
    for table in ("audit_log", "exchange_rates", "match_runs"):
        definition = _table_definition(table)
        assert "updated_at timestamptz NOT NULL DEFAULT now()," in definition
        assert re.search(r"^\s*updated_by uuid\s*,?$", definition, re.MULTILINE)
        assert not re.search(
            r"^\s*updated_by uuid\b.*\bREFERENCES\b",
            definition,
            re.MULTILINE,
        )


def test_document_email_message_id_has_root_partial_unique_index():
    documents = _table_definition("documents")
    schema = re.sub(
        r"\s+", " ", (ROOT / "database" / "001_initial_schema.sql").read_text()
    )

    assert "email_message_id varchar(300)" in documents
    assert (
        "CREATE UNIQUE INDEX uq_documents_email_message_id_root "
        "ON core.documents(email_message_id) "
        "WHERE parent_document_id IS NULL AND email_message_id IS NOT NULL"
    ) in schema


def test_reconciliation_preserves_existing_table_constraints():
    exchange_rates = _table_definition("exchange_rates")
    match_runs = _table_definition("match_runs")
    audit_log = _table_definition("audit_log")
    schema = (ROOT / "database" / "001_initial_schema.sql").read_text()

    assert "rate numeric(14,6) NOT NULL CHECK (rate > 0)" in exchange_rates
    assert (
        "CONSTRAINT uq_exchange_rate UNIQUE(from_currency, to_currency, rate_date)"
        in exchange_rates
    )
    assert "CONSTRAINT uq_match_run_no UNIQUE(invoice_id, run_no)" in match_runs
    assert (
        "invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE"
        in match_runs
    )
    assert "created_by uuid REFERENCES auth.users(id)" in audit_log
    assert (
        "CREATE INDEX ix_audit_entity_timeline "
        "ON core.audit_log(entity_type, entity_id, created_at)"
    ) in schema


def test_email_poll_state_and_parent_metadata_are_in_shared_bootstrap():
    state = _table_definition("email_poll_state")
    documents = _table_definition("documents")

    assert "mailbox varchar(300) NOT NULL" in state
    assert "folder varchar(300) NOT NULL" in state
    assert "uidvalidity integer NOT NULL" in state
    assert "last_uid integer NOT NULL DEFAULT 0" in state
    assert "last_run_at timestamptz" in state
    assert "last_result jsonb" in state
    assert "PRIMARY KEY (mailbox, folder)" in state
    assert "email_uid integer" in documents
    assert "email_body_preview text" in documents
    assert "email_eml_key varchar(500)" in documents


def test_email_message_retry_rows_are_folder_and_uid_scoped():
    messages = _table_definition("email_messages")

    assert "folder varchar(300) NOT NULL DEFAULT 'INBOX'" in messages
    assert "uid integer NOT NULL" in messages
    assert "attempts integer NOT NULL DEFAULT 0" in messages
    assert "next_retry_at timestamptz" in messages
    assert "failure_reason varchar(200)" in messages
    assert "raw_object_key varchar(500)" in messages
    assert "ix_email_messages_due_retry" in (
        ROOT / "database" / "001_initial_schema.sql"
    ).read_text()
    assert (
        "ON core.email_messages(mailbox, folder, uid)"
        in (ROOT / "database" / "001_initial_schema.sql").read_text()
    )


def test_email_draft_outbox_schema_is_in_shared_bootstrap():
    drafts = _table_definition("email_drafts")
    events = _table_definition("email_draft_events")
    assert "invoice_id uuid REFERENCES core.invoices(id)" in drafts
    assert "to_addresses jsonb NOT NULL" in drafts
    assert "blocked_by_allowlist boolean NOT NULL DEFAULT false" in drafts
    assert "status IN ('draft','approved','sent','failed','discarded')" in drafts
    assert "draft_id uuid NOT NULL REFERENCES core.email_drafts(id)" in events
    assert "'vendor_replied'" in events


def test_email_draft_migration_is_reversible_and_idempotent():
    migration = (
        ROOT / "alembic" / "versions" / "20261022_0014_email_drafts.py"
    ).read_text()
    assert 'down_revision = "20261022_0013"' in migration
    assert "CREATE TABLE IF NOT EXISTS core.email_drafts" in migration
    assert "CREATE TABLE IF NOT EXISTS core.email_draft_events" in migration
    assert "DROP TABLE IF EXISTS core.email_draft_events" in migration
    assert "DROP TABLE IF EXISTS core.email_drafts" in migration


def test_lifecycle_migration_creates_audit_fields_before_confidence_backfill():
    lifecycle = (
        ROOT / "alembic" / "versions" / "20261022_0012_invoice_lifecycle_audit.py"
    ).read_text()
    confidence = (
        ROOT
        / "alembic"
        / "versions"
        / "20261022_0013_invoice_verification_confidence.py"
    ).read_text()
    assert 'revision = "20261022_0012"' in lifecycle
    assert 'down_revision = "20261022_0011"' in lifecycle
    for column in ("reason", "step_name", "duration_ms", "input_ref"):
        assert f'("{column}",' in lifecycle
    assert "ADD COLUMN IF NOT EXISTS" in lifecycle
    assert "DROP COLUMN IF EXISTS" in lifecycle
    assert "CREATE TABLE IF NOT EXISTS core.invoice_state_history" not in confidence
    assert confidence.index("ADD COLUMN IF NOT EXISTS") < confidence.index(
        "INSERT INTO core.invoice_state_history"
    )
    assert "INSERT INTO core.invoice_state_history" in confidence
