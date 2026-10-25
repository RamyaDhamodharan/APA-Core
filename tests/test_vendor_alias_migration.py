import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from src.schemas.vendor_schemas import VendorAliasCreate

ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "20261019_0001"
ALIAS_REVISION = "20261019_0002"


def _load_alias_revision():
    revision_path = (
        ROOT / "alembic" / "versions" / f"{ALIAS_REVISION}_vendor_alias_management.py"
    )
    spec = importlib.util.spec_from_file_location(
        "vendor_alias_management_revision",
        revision_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alembic_revision_chain_has_one_expected_head():
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["20261025_0024"]
    alias_revision = script.get_revision(ALIAS_REVISION)
    assert alias_revision is not None
    assert alias_revision.down_revision == BASELINE_REVISION
    assert script.get_revision("0f7ca912d0e0").down_revision == ALIAS_REVISION
    expected_chain = (
        ("20261022_0011", "20261022_0010"),
        ("20261022_0012", "20261022_0011"),
        ("20261022_0013", "20261022_0012"),
        ("20261022_0014", "20261022_0013"),
        ("20261022_0015", "20261022_0014"),
        ("20261022_0016", "20261022_0015"),
    )
    for revision_id, down_revision in expected_chain:
        revision = script.get_revision(revision_id)
        assert revision is not None
        assert revision.down_revision == down_revision


def test_sql_bootstrap_matches_pre_alias_migration_baseline():
    schema = (ROOT / "database" / "001_initial_schema.sql").read_text()
    alias_table = schema.split("CREATE TABLE core.vendor_aliases (", 1)[1].split(
        ");",
        1,
    )[0]

    assert "alias_norm varchar(200) NOT NULL" in alias_table
    assert "is_active" not in alias_table
    assert "CREATE UNIQUE INDEX uq_vendor_alias_norm" not in schema
    assert (
        "CREATE INDEX ix_vendor_alias_norm ON core.vendor_aliases(alias_norm)"
    ) in schema


def test_alias_migration_upgrade_checks_and_normalizes_existing_data(
    monkeypatch,
):
    migration = _load_alias_revision()
    assert "global normalized-name uniqueness" in migration.__doc__
    statements = []
    fake_op = SimpleNamespace(execute=statements.append)
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()
    sql = "\n".join(statements)

    assert "ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true" in sql
    assert "Duplicate normalized vendor aliases require manual review" in sql
    assert "blank alias requires manual review" in sql
    assert sql.index("blank alias requires manual review") < sql.index(
        "Duplicate normalized vendor aliases require manual review"
    )
    assert "^[[:space:]]+|[[:space:]]+$" in sql
    assert "'[[:space:]]+'" in sql
    assert "trim(alias_norm)" not in sql
    assert "UPDATE vendor_aliases" in sql
    assert "DROP INDEX IF EXISTS ix_vendor_alias_norm" in sql
    assert "CREATE UNIQUE INDEX uq_vendor_alias_norm" in sql
    assert "ON vendor_aliases(alias_norm)\n        WHERE is_active" not in sql


def test_whitespace_only_aliases_are_rejected_by_application_schema():
    for alias in ("   ", "\t", "\n\r\t"):
        with pytest.raises(ValidationError, match="must not be blank"):
            VendorAliasCreate(alias=alias)


def test_duplicate_check_covers_normalized_active_and_inactive_aliases(
    monkeypatch,
):
    migration = _load_alias_revision()
    statements = []
    fake_op = SimpleNamespace(execute=statements.append)
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()
    sql = "\n".join(statements)
    duplicate_check = sql.split("DO $$", 1)[1].split("$$", 1)[0]

    assert "GROUP BY lower(regexp_replace(" in duplicate_check
    assert "HAVING count(*) > 1" in duplicate_check
    assert "WHERE is_active" not in duplicate_check
    assert "CREATE UNIQUE INDEX uq_vendor_alias_norm" in sql


def test_downgrade_documents_irreversible_normalization_and_preserves_aliases(
    monkeypatch,
):
    migration = _load_alias_revision()
    statements = []
    fake_op = SimpleNamespace(execute=statements.append)
    monkeypatch.setattr(migration, "op", fake_op)

    migration.downgrade()
    sql = "\n".join(statements)

    assert "original spelling and whitespace" in migration.downgrade.__doc__
    assert (
        "Cannot downgrade vendor alias management while inactive aliases exist" in sql
    )
    assert "DELETE FROM" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN IF EXISTS is_active" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_vendor_alias_norm" in sql


def test_alias_migration_downgrade_guards_data_and_restores_baseline_index(
    monkeypatch,
):
    migration = _load_alias_revision()
    statements = []
    fake_op = SimpleNamespace(execute=statements.append)
    monkeypatch.setattr(migration, "op", fake_op)

    migration.downgrade()
    sql = "\n".join(statements)

    assert (
        "Cannot downgrade vendor alias management while inactive aliases exist" in sql
    )
    assert "DROP INDEX IF EXISTS uq_vendor_alias_norm" in sql
    assert "DROP COLUMN IF EXISTS is_active" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_vendor_alias_norm" in sql
