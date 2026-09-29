"""Persist the broker pricing evidence used by Demo preflight gates."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_0024"
down_revision = "20260929_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable preflight evidence without changing execution semantics."""

    with op.batch_alter_table("demo_execution_records", schema=None) as batch:
        batch.add_column(sa.Column("executable_price", sa.Float(), nullable=True))
        batch.add_column(sa.Column("deviation_price", sa.Float(), nullable=True))
        batch.add_column(sa.Column("deviation_points", sa.Float(), nullable=True))
        batch.add_column(sa.Column("max_deviation_points", sa.Float(), nullable=True))
        batch.add_column(sa.Column("symbol_point", sa.Float(), nullable=True))
        batch.add_column(sa.Column("broker_bid", sa.Float(), nullable=True))
        batch.add_column(sa.Column("broker_ask", sa.Float(), nullable=True))
        batch.add_column(sa.Column("symbol_digits", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("broker_tick_time", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("preflight_timestamp", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("signal_created_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Remove only the additive evidence columns; historical rows remain intact."""

    with op.batch_alter_table("demo_execution_records", schema=None) as batch:
        batch.drop_column("signal_created_at")
        batch.drop_column("preflight_timestamp")
        batch.drop_column("broker_tick_time")
        batch.drop_column("symbol_digits")
        batch.drop_column("broker_ask")
        batch.drop_column("broker_bid")
        batch.drop_column("symbol_point")
        batch.drop_column("max_deviation_points")
        batch.drop_column("deviation_points")
        batch.drop_column("deviation_price")
        batch.drop_column("executable_price")
