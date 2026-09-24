"""Create the frozen Phase 1.5 observatory schema."""

import sqlalchemy as sa

from alembic import op

revision = "20260921_0001"
down_revision = None
branch_labels = None
depends_on = None


def _historical_metadata() -> sa.MetaData:
    """Return the schema snapshot from the original Phase 1.5 revision.

    This metadata is intentionally migration-local. Later schema additions are
    owned by their own revisions and must not leak in through application ORM
    evolution.
    """
    metadata = sa.MetaData()
    sa.Table(
        "accounts",
        metadata,
        sa.Column("server", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("trade_mode", sa.Integer()),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("server", "currency", "trade_mode", name="uq_account"),
    )
    sa.Table(
        "configuration_versions",
        metadata,
        sa.Column("version", sa.String(64), nullable=False, unique=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "model_versions",
        metadata,
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "performance_snapshots",
        metadata,
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("filter_context", sa.JSON()),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "prompt_versions",
        metadata,
        sa.Column("version", sa.String(64), nullable=False, unique=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("content_encrypted", sa.LargeBinary()),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "symbols",
        metadata,
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("digits", sa.Integer(), nullable=False),
        sa.Column("point", sa.Float(), nullable=False),
        sa.Column("trade_tick_size", sa.Float(), nullable=False),
        sa.Column("trade_tick_value", sa.Float(), nullable=False),
        sa.Column("trade_tick_value_profit", sa.Float(), nullable=False),
        sa.Column("trade_tick_value_loss", sa.Float(), nullable=False),
        sa.Column("contract_size", sa.Float(), nullable=False),
        sa.Column("volume_min", sa.Float(), nullable=False),
        sa.Column("volume_max", sa.Float(), nullable=False),
        sa.Column("volume_step", sa.Float(), nullable=False),
        sa.Column("trade_mode", sa.Integer(), nullable=False),
        sa.Column("trade_mode_name", sa.String(32), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_symbol_name"),
    )
    sa.Table(
        "system_events",
        metadata,
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, index=True),
        sa.Column("correlation_id", sa.String(36), index=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    sa.Table(
        "system_health",
        metadata,
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("component", sa.String(64), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("message", sa.Text()),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "account_snapshots",
        metadata,
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("balance", sa.Float(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("margin", sa.Float(), nullable=False),
        sa.Column("free_margin", sa.Float(), nullable=False),
        sa.Column("margin_level", sa.Float(), nullable=False),
        sa.Column("profit", sa.Float(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "candles",
        metadata,
        sa.Column("symbol_id", sa.String(36), sa.ForeignKey("symbols.id"), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("tick_volume", sa.BigInteger(), nullable=False),
        sa.Column("spread", sa.Integer(), nullable=False),
        sa.Column("real_volume", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.UniqueConstraint("symbol_id", "timeframe", "timestamp", name="uq_closed_candle"),
        sa.Index("ix_candle_window", "symbol_id", "timeframe", "timestamp"),
    )
    sa.Table(
        "positions",
        metadata,
        sa.Column("broker_ticket", sa.BigInteger(), nullable=False),
        sa.Column("symbol_id", sa.String(36), sa.ForeignKey("symbols.id"), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("broker_open_time", sa.DateTime(), nullable=False),
        sa.Column("closed_observed_at", sa.DateTime()),
        sa.Column("magic_number", sa.BigInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("broker_ticket", "symbol_id", name="uq_position_ticket"),
        sa.Index("ix_positions_symbol_id", "symbol_id"),
    )
    sa.Table(
        "trades",
        metadata,
        sa.Column("broker_ticket", sa.BigInteger(), index=True),
        sa.Column(
            "symbol_id", sa.String(36), sa.ForeignKey("symbols.id"), nullable=False, index=True
        ),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("entry_time", sa.DateTime()),
        sa.Column("entry_price", sa.Float()),
        sa.Column("exit_time", sa.DateTime()),
        sa.Column("exit_price", sa.Float()),
        sa.Column("initial_stop_loss", sa.Float()),
        sa.Column("initial_take_profit", sa.Float()),
        sa.Column("final_stop_loss", sa.Float()),
        sa.Column("final_take_profit", sa.Float()),
        sa.Column("volume", sa.Float()),
        sa.Column("risk_percent", sa.Float()),
        sa.Column("risk_amount", sa.Float()),
        sa.Column("initial_risk_price_distance", sa.Float()),
        sa.Column("planned_rr", sa.Float()),
        sa.Column("realized_r", sa.Float()),
        sa.Column("gross_profit", sa.Float()),
        sa.Column("net_profit", sa.Float()),
        sa.Column("commission", sa.Float()),
        sa.Column("swap", sa.Float()),
        sa.Column("fees", sa.Float()),
        sa.Column("duration_seconds", sa.BigInteger()),
        sa.Column("mae", sa.Float()),
        sa.Column("mfe", sa.Float()),
        sa.Column("mae_r", sa.Float()),
        sa.Column("mfe_r", sa.Float()),
        sa.Column("exit_reason", sa.String(64)),
        sa.Column("strategy_version", sa.String(64)),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("model_version", sa.String(64)),
        sa.Column("ai_decision_id", sa.String(36), index=True),
        sa.Column("market_regime", sa.String(64)),
        sa.Column("session", sa.String(32)),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    sa.Table(
        "market_snapshots",
        metadata,
        sa.Column(
            "symbol_id", sa.String(36), sa.ForeignKey("symbols.id"), nullable=False, index=True
        ),
        sa.Column(
            "account_snapshot_id",
            sa.String(36),
            sa.ForeignKey("account_snapshots.id"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("bid", sa.Float(), nullable=False),
        sa.Column("ask", sa.Float(), nullable=False),
        sa.Column("spread", sa.Integer(), nullable=False),
        sa.Column("candle_windows", sa.JSON(), nullable=False),
        sa.Column("latest_candles", sa.JSON(), nullable=False),
        sa.Column("technical_features", sa.JSON()),
        sa.Column("market_regime", sa.String(64)),
        sa.Column("session", sa.String(32)),
        sa.Column("relevant_news_ids", sa.JSON()),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "trade_events",
        metadata,
        sa.Column(
            "trade_id", sa.String(36), sa.ForeignKey("trades.id"), nullable=False, index=True
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("price", sa.Float()),
        sa.Column("volume", sa.Float()),
        sa.Column("stop_loss", sa.Float()),
        sa.Column("take_profit", sa.Float()),
        sa.Column("pnl", sa.Float()),
        sa.Column("reason", sa.Text()),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.UniqueConstraint("idempotency_key", name="uq_trade_event_idempotency"),
    )
    sa.Table(
        "ai_decisions",
        metadata,
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column(
            "symbol_id", sa.String(36), sa.ForeignKey("symbols.id"), nullable=False, index=True
        ),
        sa.Column("action", sa.String(16), nullable=False, index=True),
        sa.Column("confidence", sa.Float()),
        sa.Column("structured_rationale", sa.JSON(), nullable=False),
        sa.Column("market_snapshot_id", sa.String(36), sa.ForeignKey("market_snapshots.id")),
        sa.Column("risk_snapshot_id", sa.String(36)),
        sa.Column("requested_risk_percent", sa.Float()),
        sa.Column("proposed_entry", sa.Float()),
        sa.Column("proposed_sl", sa.Float()),
        sa.Column("proposed_tp", sa.Float()),
        sa.Column("decision_latency_ms", sa.Integer()),
        sa.Column("model_name", sa.String(100)),
        sa.Column("model_version", sa.String(64)),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("validation_status", sa.String(32)),
        sa.Column("execution_status", sa.String(32)),
        sa.Column("result_trade_id", sa.String(36), sa.ForeignKey("trades.id")),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "position_snapshots",
        metadata,
        sa.Column(
            "position_id", sa.String(36), sa.ForeignKey("positions.id"), nullable=False, index=True
        ),
        sa.Column(
            "market_snapshot_id",
            sa.String(36),
            sa.ForeignKey("market_snapshots.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("open_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("profit", sa.Float(), nullable=False),
        sa.Column("swap", sa.Float(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "risk_snapshots",
        metadata,
        sa.Column("market_snapshot_id", sa.String(36), sa.ForeignKey("market_snapshots.id")),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("balance", sa.Float(), nullable=False),
        sa.Column("open_risk_percent", sa.Float()),
        sa.Column("open_risk_amount", sa.Float()),
        sa.Column("remaining_risk_percent", sa.Float()),
        sa.Column("risk_per_position", sa.JSON(), nullable=False),
        sa.Column("daily_pnl", sa.Float()),
        sa.Column("daily_realized_loss", sa.Float()),
        sa.Column("drawdown_percent", sa.Float()),
        sa.Column("max_trade_risk_percent", sa.Float(), nullable=False),
        sa.Column("max_aggregate_risk_percent", sa.Float(), nullable=False),
        sa.Column("open_positions_count", sa.Integer(), nullable=False),
        sa.Column("unbounded_positions_count", sa.Integer(), nullable=False),
        sa.Column("margin_usage_percent", sa.Float()),
        sa.Column("free_margin", sa.Float(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
    )
    return metadata


def upgrade() -> None:
    _historical_metadata().create_all(bind=op.get_bind())


def downgrade() -> None:
    _historical_metadata().drop_all(bind=op.get_bind())
