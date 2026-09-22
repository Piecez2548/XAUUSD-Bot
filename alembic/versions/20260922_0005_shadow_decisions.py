"""Persist deterministic Phase 2 shadow decisions."""

import sqlalchemy as sa

from alembic import op

revision = "20260922_0005"
down_revision = "20260922_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "shadow_decisions" in inspector.get_table_names():
        return
    op.create_table(
        "shadow_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("market_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("m5_candle_timestamp", sa.DateTime(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("market_regime", sa.String(length=32), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("take_profit", sa.Float(), nullable=True),
        sa.Column("risk_reward_ratio", sa.Float(), nullable=True),
        sa.Column("requested_risk_percent", sa.Float(), nullable=True),
        sa.Column("approved_risk_percent", sa.Float(), nullable=True),
        sa.Column("hypothetical_volume", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("human_readable_reason", sa.Text(), nullable=False),
        sa.Column("risk_gate_state", sa.String(length=32), nullable=False),
        sa.Column("data_freshness", sa.String(length=32), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("feature_context", sa.JSON(), nullable=False),
        sa.Column("outcome_status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol",
            "m5_candle_timestamp",
            "strategy_version",
            name="uq_shadow_decision_candle",
        ),
    )
    op.create_index("ix_shadow_decisions_timestamp", "shadow_decisions", ["created_at"])
    op.create_index("ix_shadow_decisions_symbol", "shadow_decisions", ["symbol"])
    op.create_index("ix_shadow_decisions_snapshot", "shadow_decisions", ["market_snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_shadow_decisions_snapshot", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_symbol", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_timestamp", table_name="shadow_decisions")
    op.drop_table("shadow_decisions")
