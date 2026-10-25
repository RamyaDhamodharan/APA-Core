"""Mark the established shared database schema as the AP-Core baseline.

This is a no-op marker. It records the baseline but does not create or validate
the schema. Fresh databases use database/001_initial_schema.sql; existing
databases must be reconciled with that shared-schema contract before stamping.
"""

revision = "20261019_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
