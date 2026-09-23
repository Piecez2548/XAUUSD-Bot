"""Add immutable Strategy Intelligence provenance and forward linkage."""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0012"
down_revision = "20260923_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_intelligence_records", schema=None) as batch:
        batch.add_column(sa.Column("intelligence_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("intelligence_runtime_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("pair_zone_event_id", sa.String(100), nullable=True))
        batch.add_column(sa.Column("forward_trade_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_strategy_intelligence_forward_trade",
            "forward_validation_trades",
            ["forward_trade_id"],
            ["id"],
        )

    op.create_index(
        "ix_strategy_intelligence_pair_zone_event_id",
        "strategy_intelligence_records",
        ["pair_zone_event_id"],
    )
    op.create_index(
        "ix_strategy_intelligence_forward_trade_id",
        "strategy_intelligence_records",
        ["forward_trade_id"],
        unique=True,
    )
    op.create_index(
        "uq_strategy_intelligence_forward_signal",
        "strategy_intelligence_records",
        ["forward_signal_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_strategy_intelligence_forward_signal", table_name="strategy_intelligence_records"
    )
    op.drop_index(
        "ix_strategy_intelligence_forward_trade_id", table_name="strategy_intelligence_records"
    )
    op.drop_index(
        "ix_strategy_intelligence_pair_zone_event_id", table_name="strategy_intelligence_records"
    )
    with op.batch_alter_table("strategy_intelligence_records", schema=None) as batch:
        batch.drop_column("forward_trade_id")
        batch.drop_column("pair_zone_event_id")
        batch.drop_column("intelligence_runtime_version")
        batch.drop_column("intelligence_version")
