"""Portable SQLAlchemy schema for observability and future trade audit history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("database timestamps must be timezone-aware")
        normalized = value.astimezone(UTC)
        return normalized.replace(tzinfo=None) if dialect.name == "sqlite" else normalized

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class AccountRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("server", "currency", "trade_mode", name="uq_account"),)

    server: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_mode: Mapped[int | None] = mapped_column(Integer)


class AccountSnapshotRecord(IdMixin, Base):
    __tablename__ = "account_snapshots"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    free_margin: Mapped[float] = mapped_column(Float)
    margin_level: Mapped[float] = mapped_column(Float)
    profit: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)


class SymbolRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "symbols"
    __table_args__ = (UniqueConstraint("name", name="uq_symbol_name"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    digits: Mapped[int] = mapped_column(Integer)
    point: Mapped[float] = mapped_column(Float)
    trade_tick_size: Mapped[float] = mapped_column(Float)
    trade_tick_value: Mapped[float] = mapped_column(Float)
    trade_tick_value_profit: Mapped[float] = mapped_column(Float)
    trade_tick_value_loss: Mapped[float] = mapped_column(Float)
    contract_size: Mapped[float] = mapped_column(Float)
    volume_min: Mapped[float] = mapped_column(Float)
    volume_max: Mapped[float] = mapped_column(Float)
    volume_step: Mapped[float] = mapped_column(Float)
    trade_mode: Mapped[int] = mapped_column(Integer)
    trade_mode_name: Mapped[str] = mapped_column(String(32))


class CandleRecord(IdMixin, Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol_id", "timeframe", "timestamp", name="uq_closed_candle"),
        Index("ix_candle_window", "symbol_id", "timeframe", "timestamp"),
    )

    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"))
    timeframe: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime())
    raw_timestamp: Mapped[int] = mapped_column(BigInteger)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    tick_volume: Mapped[int] = mapped_column(BigInteger)
    spread: Mapped[int] = mapped_column(Integer)
    real_volume: Mapped[int] = mapped_column(BigInteger)


class CandleCursorRecord(IdMixin, Base):
    __tablename__ = "candle_cursors"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", name="uq_candle_cursor"),)

    symbol: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    last_completed_timestamp: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, onupdate=utc_now)


class MarketSnapshotRecord(IdMixin, Base):
    __tablename__ = "market_snapshots"

    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    account_snapshot_id: Mapped[str] = mapped_column(ForeignKey("account_snapshots.id"))
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    spread: Mapped[int] = mapped_column(Integer)
    positions_observed_successfully: Mapped[bool] = mapped_column(Boolean, default=False)
    open_position_count: Mapped[int] = mapped_column(Integer, default=0)
    candle_windows: Mapped[dict[str, Any]] = mapped_column(JSON)
    latest_candles: Mapped[dict[str, Any]] = mapped_column(JSON)
    technical_features: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    market_regime: Mapped[str | None] = mapped_column(String(64))
    session: Mapped[str | None] = mapped_column(String(32))
    relevant_news_ids: Mapped[list[str] | None] = mapped_column(JSON)


class PositionRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("broker_ticket", "symbol_id", name="uq_position_ticket"),)

    broker_ticket: Mapped[int] = mapped_column(BigInteger)
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime())
    broker_open_time: Mapped[datetime] = mapped_column(UtcDateTime())
    closed_observed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    magic_number: Mapped[int] = mapped_column(BigInteger)
    comment: Mapped[str] = mapped_column(Text)


class PositionSnapshotRecord(IdMixin, Base):
    __tablename__ = "position_snapshots"

    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"), index=True)
    market_snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    volume: Mapped[float] = mapped_column(Float)
    open_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    profit: Mapped[float] = mapped_column(Float)
    swap: Mapped[float] = mapped_column(Float)


class TradeRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "trades"

    broker_ticket: Mapped[int | None] = mapped_column(BigInteger, index=True)
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    entry_time: Mapped[datetime | None] = mapped_column(UtcDateTime())
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(UtcDateTime())
    exit_price: Mapped[float | None] = mapped_column(Float)
    initial_stop_loss: Mapped[float | None] = mapped_column(Float)
    initial_take_profit: Mapped[float | None] = mapped_column(Float)
    final_stop_loss: Mapped[float | None] = mapped_column(Float)
    final_take_profit: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    risk_percent: Mapped[float | None] = mapped_column(Float)
    risk_amount: Mapped[float | None] = mapped_column(Float)
    initial_risk_price_distance: Mapped[float | None] = mapped_column(Float)
    planned_rr: Mapped[float | None] = mapped_column(Float)
    realized_r: Mapped[float | None] = mapped_column(Float)
    gross_profit: Mapped[float | None] = mapped_column(Float)
    net_profit: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float | None] = mapped_column(Float)
    swap: Mapped[float | None] = mapped_column(Float)
    fees: Mapped[float | None] = mapped_column(Float)
    duration_seconds: Mapped[int | None] = mapped_column(BigInteger)
    mae: Mapped[float | None] = mapped_column(Float)
    mfe: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)
    mfe_r: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    strategy_version: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    ai_decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    market_regime: Mapped[str | None] = mapped_column(String(64))
    session: Mapped[str | None] = mapped_column(String(32))


class TradeEventRecord(IdMixin, Base):
    __tablename__ = "trade_events"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_trade_event_idempotency"),)

    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    price: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))


class BrokerDealRecord(IdMixin, Base):
    __tablename__ = "broker_deals"
    __table_args__ = (
        UniqueConstraint("deal_ticket", name="uq_broker_deal_ticket"),
        Index("ix_broker_deal_position_time", "position_id", "timestamp"),
        Index("ix_broker_deal_symbol_time", "symbol", "timestamp"),
    )

    deal_ticket: Mapped[int] = mapped_column(BigInteger)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    deal_type: Mapped[str | None] = mapped_column(String(32))
    entry_type: Mapped[str | None] = mapped_column(String(32))
    volume: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    profit: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float | None] = mapped_column(Float)
    swap: Mapped[float | None] = mapped_column(Float)
    fee: Mapped[float | None] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(Text)
    magic_number: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(64))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class HistoryCursorRecord(IdMixin, Base):
    __tablename__ = "history_cursors"
    __table_args__ = (UniqueConstraint("scope", name="uq_history_cursor_scope"),)

    scope: Mapped[str] = mapped_column(String(64))
    last_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_identifier: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, onupdate=utc_now)


class AiDecisionRecord(IdMixin, Base):
    __tablename__ = "ai_decisions"

    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    structured_rationale: Mapped[dict[str, Any]] = mapped_column(JSON)
    market_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("market_snapshots.id"))
    risk_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    requested_risk_percent: Mapped[float | None] = mapped_column(Float)
    proposed_entry: Mapped[float | None] = mapped_column(Float)
    proposed_sl: Mapped[float | None] = mapped_column(Float)
    proposed_tp: Mapped[float | None] = mapped_column(Float)
    decision_latency_ms: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str | None] = mapped_column(String(100))
    model_version: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    validation_status: Mapped[str | None] = mapped_column(String(32))
    execution_status: Mapped[str | None] = mapped_column(String(32))
    result_trade_id: Mapped[str | None] = mapped_column(ForeignKey("trades.id"))


class ShadowDecisionRecord(IdMixin, Base):
    """Append-only deterministic Phase 2 decision record; never an execution request."""

    __tablename__ = "shadow_decisions"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "m5_candle_timestamp",
            "strategy_version",
            name="uq_shadow_decision_candle",
        ),
        Index("ix_shadow_decisions_timestamp", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    market_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_snapshots.id"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    m5_candle_timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    decision: Mapped[str] = mapped_column(String(16), index=True)
    market_regime: Mapped[str] = mapped_column(String(32))
    entry_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    risk_reward_ratio: Mapped[float | None] = mapped_column(Float)
    requested_risk_percent: Mapped[float | None] = mapped_column(Float)
    approved_risk_percent: Mapped[float | None] = mapped_column(Float)
    hypothetical_volume: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    strategy_name: Mapped[str] = mapped_column(String(100))
    strategy_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON)
    human_readable_reason: Mapped[str] = mapped_column(Text)
    risk_gate_state: Mapped[str] = mapped_column(String(32))
    data_freshness: Mapped[str] = mapped_column(String(32))
    execution_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    feature_context: Mapped[dict[str, Any]] = mapped_column(JSON)
    outcome_status: Mapped[str] = mapped_column(String(32), default="PENDING")


class StrategyActivationRecord(IdMixin, Base):
    """Auditable shadow-only strategy activation at a closed-candle boundary."""

    __tablename__ = "strategy_activations"
    __table_args__ = (Index("ix_strategy_activations_effective", "effective_from_m5"),)

    strategy_id: Mapped[str] = mapped_column(String(100), index=True)
    strategy_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    effective_from_m5: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    previous_strategy: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    execution_allowed: Mapped[bool] = mapped_column(Boolean, default=False)


class ShadowOutcomeRecord(IdMixin, Base):
    """Durable, deterministic evaluation of one hypothetical trade."""

    __tablename__ = "shadow_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            "evaluation_policy_version",
            name="uq_shadow_outcome_policy",
        ),
        Index("ix_shadow_outcomes_terminal_time", "terminal_candle_timestamp"),
        Index("ix_shadow_outcomes_status", "terminal_status"),
    )

    decision_id: Mapped[str] = mapped_column(ForeignKey("shadow_decisions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[str] = mapped_column(String(64), index=True)
    evaluation_policy_version: Mapped[str] = mapped_column(String(64), index=True)
    decision_m5_timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    side: Mapped[str] = mapped_column(String(16))
    entry_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    initial_risk_distance: Mapped[float | None] = mapped_column(Float)
    target_r_multiple: Mapped[float | None] = mapped_column(Float)
    evaluation_started_at: Mapped[datetime] = mapped_column(UtcDateTime())
    terminal_candle_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime(), index=True)
    terminal_status: Mapped[str] = mapped_column(String(32), index=True)
    exit_price: Mapped[float | None] = mapped_column(Float)
    realized_r: Mapped[float | None] = mapped_column(Float)
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    max_favorable_excursion_price: Mapped[float | None] = mapped_column(Float)
    max_adverse_excursion_price: Mapped[float | None] = mapped_column(Float)
    mfe_r: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    evaluated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    reason_code: Mapped[str] = mapped_column(String(64))
    source_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_snapshots.id"), index=True
    )


class RiskSnapshotRecord(IdMixin, Base):
    __tablename__ = "risk_snapshots"

    market_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("market_snapshots.id"))
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    equity: Mapped[float] = mapped_column(Float)
    balance: Mapped[float] = mapped_column(Float)
    open_risk_percent: Mapped[float | None] = mapped_column(Float)
    open_risk_amount: Mapped[float | None] = mapped_column(Float)
    remaining_risk_percent: Mapped[float | None] = mapped_column(Float)
    risk_per_position: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    daily_pnl: Mapped[float | None] = mapped_column(Float)
    daily_realized_loss: Mapped[float | None] = mapped_column(Float)
    drawdown_percent: Mapped[float | None] = mapped_column(Float)
    max_trade_risk_percent: Mapped[float] = mapped_column(Float)
    max_aggregate_risk_percent: Mapped[float] = mapped_column(Float)
    open_positions_count: Mapped[int] = mapped_column(Integer)
    unbounded_positions_count: Mapped[int] = mapped_column(Integer)
    margin_usage_percent: Mapped[float | None] = mapped_column(Float)
    free_margin: Mapped[float] = mapped_column(Float)


class PerformanceSnapshotRecord(IdMixin, Base):
    __tablename__ = "performance_snapshots"

    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON)
    filter_context: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SystemEventRecord(Base):
    __tablename__ = "system_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    source: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    schema_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)


class SystemHealthRecord(IdMixin, Base):
    __tablename__ = "system_health"

    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    component: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ControlAuditRecord(IdMixin, Base):
    """Minimal audit record for authenticated Telegram control commands."""

    __tablename__ = "control_audit"

    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    command: Mapped[str] = mapped_column(String(32), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)


class ConfigurationVersionRecord(IdMixin, Base):
    __tablename__ = "configuration_versions"

    version: Mapped[str] = mapped_column(String(64), unique=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelVersionRecord(IdMixin, Base):
    __tablename__ = "model_versions"

    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PromptVersionRecord(IdMixin, Base):
    __tablename__ = "prompt_versions"

    version: Mapped[str] = mapped_column(String(64), unique=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    checksum: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    content_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
