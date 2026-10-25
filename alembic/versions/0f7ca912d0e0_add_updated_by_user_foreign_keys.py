"""Add updated_by foreign keys to users.

Revision ID: 0f7ca912d0e0
Revises: 20261019_0002
"""

from alembic import op

revision = "0f7ca912d0e0"
down_revision = "20261019_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_audit_log_updated_by_users",
        "audit_log",
        "users",
        ["updated_by"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_exchange_rates_updated_by_users",
        "exchange_rates",
        "users",
        ["updated_by"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_match_runs_updated_by_users",
        "match_runs",
        "users",
        ["updated_by"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_match_runs_updated_by_users", "match_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_exchange_rates_updated_by_users",
        "exchange_rates",
        type_="foreignkey",
    )
    op.drop_constraint("fk_audit_log_updated_by_users", "audit_log", type_="foreignkey")
