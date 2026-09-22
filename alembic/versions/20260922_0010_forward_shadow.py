"""Persist forward-only shadow validation sessions, signals, and trades."""

import sqlalchemy as sa

from alembic import op

revision = "20260922_0010"
down_revision = "20260922_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forward_validation_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("strategy_config_hash", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("source_identity", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframes_json", sa.JSON(), nullable=False),
        sa.Column("rr", sa.Float(), nullable=False),
        sa.Column("cost_policy_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_reason", sa.String(128)),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_forward_session_id"),
    )
    op.create_index("ix_forward_sessions_session_id", "forward_validation_sessions", ["session_id"])
    op.create_index(
        "ix_forward_sessions_strategy_id", "forward_validation_sessions", ["strategy_id"]
    )
    op.create_index("ix_forward_sessions_status", "forward_validation_sessions", ["status"])
    op.create_index("ix_forward_sessions_started", "forward_validation_sessions", ["started_at"])
    op.create_table(
        "forward_validation_signals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("signal_id", sa.String(100), nullable=False),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("forward_validation_sessions.id"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("zone_id", sa.String(100), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("risk_distance", sa.Float(), nullable=False),
        sa.Column("rr", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("pair_first_timestamp", sa.DateTime()),
        sa.Column("pair_second_timestamp", sa.DateTime()),
        sa.Column("h1_context_json", sa.JSON(), nullable=False),
        sa.Column("confirmation_candle_json", sa.JSON(), nullable=False),
        sa.Column("market_observation_json", sa.JSON(), nullable=False),
        sa.Column("strategy_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("signal_id", name="uq_forward_signal_id"),
        sa.UniqueConstraint("session_id", "timestamp", name="uq_forward_signal_candle"),
    )
    op.create_index("ix_forward_signals_signal_id", "forward_validation_signals", ["signal_id"])
    op.create_index("ix_forward_signals_session_id", "forward_validation_signals", ["session_id"])
    op.create_index(
        "ix_forward_signals_session_time", "forward_validation_signals", ["session_id", "timestamp"]
    )
    op.create_index("ix_forward_signals_decision", "forward_validation_signals", ["decision"])
    op.create_table(
        "forward_validation_trades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trade_id", sa.String(100), nullable=False),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("forward_validation_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "signal_id",
            sa.String(36),
            sa.ForeignKey("forward_validation_signals.id"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("risk_distance", sa.Float(), nullable=False),
        sa.Column("terminal_timestamp", sa.DateTime()),
        sa.Column("mark_price", sa.Float()),
        sa.Column("gross_r", sa.Float()),
        sa.Column("net_r", sa.Float()),
        sa.Column("bars_held", sa.Integer(), nullable=False),
        sa.Column("minutes_held", sa.Float()),
        sa.Column("mfe_price", sa.Float()),
        sa.Column("mae_price", sa.Float()),
        sa.Column("mfe_r", sa.Float()),
        sa.Column("mae_r", sa.Float()),
        sa.Column("spread_points", sa.Float(), nullable=False),
        sa.Column("spread_observation", sa.String(16), nullable=False),
        sa.Column("entry_slippage_points", sa.Float(), nullable=False),
        sa.Column("exit_slippage_points", sa.Float(), nullable=False),
        sa.Column("commission_r", sa.Float(), nullable=False),
        sa.Column("total_cost_r", sa.Float()),
        sa.Column("evaluated_at", sa.DateTime()),
        sa.Column("reason_code", sa.String(96)),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("trade_id", name="uq_forward_trade_id"),
        sa.UniqueConstraint("signal_id", name="uq_forward_trade_signal"),
    )
    op.create_index("ix_forward_trades_trade_id", "forward_validation_trades", ["trade_id"])
    op.create_index("ix_forward_trades_session_id", "forward_validation_trades", ["session_id"])
    op.create_index(
        "ix_forward_trades_session_state", "forward_validation_trades", ["session_id", "state"]
    )
    op.create_index("ix_forward_trades_side", "forward_validation_trades", ["side"])


def downgrade() -> None:
    op.drop_table("forward_validation_trades")
    op.drop_table("forward_validation_signals")
    op.drop_table("forward_validation_sessions")
