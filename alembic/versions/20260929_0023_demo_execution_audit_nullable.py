"""Allow Momentum Demo audit rows without Pair Zone provenance."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_0023"
down_revision = "20260928_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Make the legacy Pair Zone identity optional for Momentum records."""

    with op.batch_alter_table("demo_execution_records", schema=None) as batch:
        batch.alter_column(
            "pair_zone_event_id",
            existing_type=sa.String(length=100),
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    """Restore the legacy constraint only when no NULL identities exist."""

    bind = op.get_bind()
    null_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM demo_execution_records "
            "WHERE pair_zone_event_id IS NULL"
        )
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            "Cannot downgrade demo execution audit schema while Momentum records "
            "without pair_zone_event_id exist"
        )

    with op.batch_alter_table("demo_execution_records", schema=None) as batch:
        batch.alter_column(
            "pair_zone_event_id",
            existing_type=sa.String(length=100),
            existing_nullable=True,
            nullable=False,
        )
