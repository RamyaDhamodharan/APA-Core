"""Normalize vendor aliases and enforce global normalized-name uniqueness.

Revision ID: 20261019_0002
Revises: 20261019_0001
"""

from alembic import op

revision = "20261019_0002"
down_revision = "20261019_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vendor_aliases
        ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true
        """
    )
    op.execute(
        r"""
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM vendor_aliases
            WHERE lower(regexp_replace(
              regexp_replace(
                alias_norm,
                '^[[:space:]]+|[[:space:]]+$',
                '',
                'g'
              ),
              '[[:space:]]+',
              ' ',
              'g'
            )) = ''
          ) THEN
            RAISE EXCEPTION
              'Cannot normalize vendor aliases: blank alias requires manual review';
          END IF;
          IF EXISTS (
            SELECT lower(regexp_replace(
              regexp_replace(
                alias_norm,
                '^[[:space:]]+|[[:space:]]+$',
                '',
                'g'
              ),
              '[[:space:]]+',
              ' ',
              'g'
            ))
            FROM vendor_aliases
            GROUP BY lower(regexp_replace(
              regexp_replace(
                alias_norm,
                '^[[:space:]]+|[[:space:]]+$',
                '',
                'g'
              ),
              '[[:space:]]+',
              ' ',
              'g'
            ))
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'Duplicate normalized vendor aliases require manual review';
          END IF;
        END;
        $$
        """
    )
    op.execute(
        r"""
        UPDATE vendor_aliases
        SET alias_norm = lower(regexp_replace(
          regexp_replace(
            alias_norm,
            '^[[:space:]]+|[[:space:]]+$',
            '',
            'g'
          ),
          '[[:space:]]+',
          ' ',
          'g'
        ))
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_vendor_alias_norm")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_vendor_alias_norm
        ON vendor_aliases(alias_norm)
        """
    )


def downgrade() -> None:
    """Restore the pre-alias-management schema without removing alias rows.

    The original spelling and whitespace of normalized alias values cannot
    be restored during downgrade.
    """
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM vendor_aliases WHERE is_active IS FALSE
          ) THEN
            RAISE EXCEPTION
              'Cannot downgrade vendor alias management while inactive aliases exist';
          END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_vendor_alias_norm")
    op.execute("ALTER TABLE vendor_aliases DROP COLUMN IF EXISTS is_active")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_vendor_alias_norm
        ON vendor_aliases(alias_norm)
        """
    )
