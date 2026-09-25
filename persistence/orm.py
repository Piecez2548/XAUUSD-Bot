"""Portable SQLAlchemy schema for observability and future trade audit history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
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


# These tables are owned exclusively by Alembic auth revisions through 0015.
# Legacy create_all bootstraps must not create them ahead of that revision.
AUTH_SCHEMA_TABLE_NAMES = frozenset(
    {"auth_users", "auth_sessions", "auth_audit_events", "auth_enrollments"}
)


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

    market_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_snapshots.id"), index=True
    )
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


class AuthUserRecord(TimestampMixin, Base):
    """Application-authenticated users; account creation is local/admin-only."""

    __tablename__ = "auth_users"
    __table_args__ = (
        UniqueConstraint("normalized_login", name="uq_auth_users_normalized_login"),
        CheckConstraint("role IN ('OWNER', 'ADMIN')", name="ck_auth_users_role"),
        CheckConstraint(
            "state IN ('PROVISIONED', 'PENDING_APPROVAL', 'ACTIVE', 'LOCKED', 'REVOKED')",
            name="ck_auth_users_state",
        ),
        Index("ix_auth_users_bound_tailscale_login", "bound_tailscale_login"),
        Index(
            "uq_auth_users_single_owner",
            "role",
            unique=True,
            sqlite_where=text("role = 'OWNER'"),
            postgresql_where=text("role = 'OWNER'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    normalized_login: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    bound_tailscale_login: Mapped[str] = mapped_column(String(254), nullable=False)


class AuthSessionRecord(Base):
    """Revocable opaque browser session; only the token hash is persisted."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_idle_expires_at", "idle_expires_at"),
        Index("ix_auth_sessions_absolute_expires_at", "absolute_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    idle_expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class AuthAuditRecord(Base):
    """Minimal auth-core audit; never stores submitted or bearer secrets."""

    __tablename__ = "auth_audit_events"
    __table_args__ = (
        Index("ix_auth_audit_events_timestamp", "timestamp"),
        Index("ix_auth_audit_events_user_id", "user_id"),
        Index("ix_auth_audit_events_event_type", "event_type"),
        Index("ix_auth_audit_events_actor_user_id", "actor_user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Intentionally denormalized safe provenance: audit history survives
    # account retention/deletion and remains additive on SQLite.
    actor_user_id: Mapped[str | None] = mapped_column(String(36))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("auth_users.id", ondelete="SET NULL"))
    normalized_login: Mapped[str | None] = mapped_column(String(254))
    tailscale_login: Mapped[str | None] = mapped_column(String(254))
    session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))


class AuthEnrollmentRecord(Base):
    """Single-use, expiring enrollment secret stored only as a digest."""

    __tablename__ = "auth_enrollments"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_enrollments_token_hash"),
        Index("ix_auth_enrollments_user_id", "user_id"),
        Index("ix_auth_enrollments_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("auth_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


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


class ResearchDatasetRecord(IdMixin, Base):
    """Immutable, provenance-bearing dataset manifest for offline research."""

    __tablename__ = "research_datasets"
    __table_args__ = (
        UniqueConstraint("dataset_hash", name="uq_research_dataset_hash"),
        Index("ix_research_datasets_symbol_timeframe", "symbol", "timeframe"),
    )

    dataset_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    start_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    end_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(32), nullable=False, default="UTC")
    closed_candles_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ResearchCandleRecord(IdMixin, Base):
    """Copied closed candle rows; research never mutates operational candles."""

    __tablename__ = "research_candles"
    __table_args__ = (
        UniqueConstraint("dataset_id", "timestamp", name="uq_research_candle"),
        Index("ix_research_candles_dataset_time", "dataset_id", "timestamp"),
    )

    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    raw_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    tick_volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spread: Mapped[int] = mapped_column(Integer, nullable=False)
    real_volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ResearchRunRecord(IdMixin, Base):
    """Persistent run manifest and aggregate result for deterministic backtests."""

    __tablename__ = "research_runs"
    __table_args__ = (Index("ix_research_runs_created", "created_at"),)

    run_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    run_name: Mapped[str | None] = mapped_column(String(120))
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id"), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    git_dirty: Mapped[bool | None] = mapped_column(Boolean)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    split_definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ResearchDecisionRecord(IdMixin, Base):
    __tablename__ = "research_decisions"
    __table_args__ = (Index("ix_research_decisions_run_time", "run_id", "timestamp"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    risk_reward_ratio: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    feature_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rejection_stage: Mapped[str | None] = mapped_column(String(64))
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ResearchOutcomeRecord(IdMixin, Base):
    __tablename__ = "research_outcomes"
    __table_args__ = (
        UniqueConstraint("decision_id", "policy_version", name="uq_research_outcome_policy"),
    )

    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("research_decisions.id"), index=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    terminal_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    realized_r: Mapped[float | None] = mapped_column(Float)
    bars_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mfe_r: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)
    terminal_candle_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)


class ResearchMetricRecord(IdMixin, Base):
    __tablename__ = "research_metrics"
    __table_args__ = (UniqueConstraint("run_id", "metric_key", name="uq_research_metric"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    metric_key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    denominator: Mapped[int | None] = mapped_column(Integer)
    dimension_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ResearchRobustnessRecord(IdMixin, Base):
    """Immutable, research-only robustness manifest and aggregate evidence."""

    __tablename__ = "research_robustness_runs"
    __table_args__ = (Index("ix_research_robustness_created", "created_at"),)

    robustness_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id"), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    simulation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)


class ForwardValidationSessionRecord(IdMixin, Base):
    """Durable, forward-only shadow validation session manifest."""

    __tablename__ = "forward_validation_sessions"
    __table_args__ = (Index("ix_forward_sessions_started", "started_at"),)

    session_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rr: Mapped[float] = mapped_column(Float, nullable=False)
    cost_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    error_reason: Mapped[str | None] = mapped_column(String(128))
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class PairZoneEvaluationRecord(Base):
    """Latest canonical Pair Zone observation for one forward session.

    The row is replaced on each evaluation rather than appending per-candle
    history. A generation identifier prevents an earlier Live worker result
    from being mistaken for the current worker's state.
    """

    __tablename__ = "pair_zone_evaluations"
    __table_args__ = (
        Index("ix_pair_zone_evaluations_generation", "runtime_generation_id"),
    )

    forward_session_id: Mapped[str] = mapped_column(
        ForeignKey("forward_validation_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    runtime_generation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evaluation_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    evaluated_m5_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    evaluated_m15_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(8))
    zone_id: Mapped[str | None] = mapped_column(String(100))
    zone_lower: Mapped[float | None] = mapped_column(Float)
    zone_upper: Mapped[float | None] = mapped_column(Float)


class ForwardSignalRecord(IdMixin, Base):
    """One idempotent canonical signal generated after forward activation."""

    __tablename__ = "forward_validation_signals"
    __table_args__ = (
        UniqueConstraint("session_id", "timestamp", name="uq_forward_signal_candle"),
        Index("ix_forward_signals_session_time", "session_id", "timestamp"),
    )

    signal_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("forward_validation_sessions.id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    zone_id: Mapped[str] = mapped_column(String(100), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    risk_distance: Mapped[float] = mapped_column(Float, nullable=False)
    rr: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    pair_first_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    pair_second_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    h1_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    confirmation_candle_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    market_observation_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ForwardTradeRecord(IdMixin, Base):
    """Virtual forward trade and causal closed-candle outcome."""

    __tablename__ = "forward_validation_trades"
    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_forward_trade_signal"),
        Index("ix_forward_trades_session_state", "session_id", "state"),
    )

    trade_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("forward_validation_sessions.id"), nullable=False
    )
    signal_id: Mapped[str] = mapped_column(
        ForeignKey("forward_validation_signals.id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    risk_distance: Mapped[float] = mapped_column(Float, nullable=False)
    terminal_timestamp: Mapped[datetime | None] = mapped_column(UtcDateTime())
    mark_price: Mapped[float | None] = mapped_column(Float)
    gross_r: Mapped[float | None] = mapped_column(Float)
    net_r: Mapped[float | None] = mapped_column(Float)
    bars_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes_held: Mapped[float | None] = mapped_column(Float)
    mfe_price: Mapped[float | None] = mapped_column(Float)
    mae_price: Mapped[float | None] = mapped_column(Float)
    mfe_r: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)
    spread_points: Mapped[float] = mapped_column(Float, nullable=False)
    spread_observation: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_slippage_points: Mapped[float] = mapped_column(Float, nullable=False)
    exit_slippage_points: Mapped[float] = mapped_column(Float, nullable=False)
    commission_r: Mapped[float] = mapped_column(Float, nullable=False)
    total_cost_r: Mapped[float | None] = mapped_column(Float)
    evaluated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    reason_code: Mapped[str | None] = mapped_column(String(96))
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DemoExecutionControlRecord(IdMixin, TimestampMixin, Base):
    """Persisted Demo-only execution arm/kill-switch state."""

    __tablename__ = "demo_execution_controls"
    __table_args__ = (UniqueConstraint("control_key", name="uq_demo_execution_control_key"),)

    control_key: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class DemoExecutionRecord(IdMixin, TimestampMixin, Base):
    """One persistent, idempotent Demo execution attempt for a canonical signal."""

    __tablename__ = "demo_execution_records"
    __table_args__ = (
        UniqueConstraint("forward_signal_id", name="uq_demo_execution_forward_signal"),
        Index("ix_demo_execution_status_created", "status", "created_at"),
        Index("ix_demo_execution_position", "broker_position_ticket"),
    )

    forward_signal_id: Mapped[str] = mapped_column(
        ForeignKey("forward_validation_signals.id"), nullable=False
    )
    forward_session_id: Mapped[str] = mapped_column(
        ForeignKey("forward_validation_sessions.id"), nullable=False
    )
    intelligence_candidate_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    pair_zone_event_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="DEMO")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(128))
    gate_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    planned_entry: Mapped[float | None] = mapped_column(Float)
    submitted_entry: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    risk_percent: Mapped[float | None] = mapped_column(Float)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    broker_retcode: Mapped[int | None] = mapped_column(Integer)
    broker_order_ticket: Mapped[int | None] = mapped_column(BigInteger, index=True)
    broker_deal_ticket: Mapped[int | None] = mapped_column(BigInteger, index=True)
    broker_position_ticket: Mapped[int | None] = mapped_column(BigInteger, index=True)
    broker_comment: Mapped[str | None] = mapped_column(String(255))
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    acknowledged_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    terminal_outcome_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    terminal_status: Mapped[str | None] = mapped_column(String(32))


class StrategyIntelligenceRecord(IdMixin, Base):
    """Versioned, append-only Phase 3 evidence snapshot.

    This table is separate from historical Forward Shadow rows.  New records
    may reference a forward session/signal, but existing history is never
    rewritten when the intelligence schema evolves.
    """

    __tablename__ = "strategy_intelligence_records"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_strategy_intelligence_candidate"),
        UniqueConstraint("forward_signal_id", name="uq_strategy_intelligence_forward_signal"),
        UniqueConstraint("forward_trade_id", name="uq_strategy_intelligence_forward_trade"),
        Index("ix_strategy_intelligence_symbol_time", "symbol", "detected_at"),
        Index("ix_strategy_intelligence_alert", "alert_decision", "detected_at"),
    )

    candidate_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    intelligence_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intelligence_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_version: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_band: Mapped[str] = mapped_column(String(16), nullable=False)
    alert_decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    blockers_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    score_components_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    pair_zone_event_id: Mapped[str | None] = mapped_column(String(100), index=True)
    forward_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("forward_validation_sessions.id"), index=True
    )
    forward_signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("forward_validation_signals.id"), index=True
    )
    forward_trade_id: Mapped[str | None] = mapped_column(
        ForeignKey("forward_validation_trades.id"), index=True
    )
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)


class ModelInferenceEvaluationRecord(IdMixin, Base):
    """Versioned offline advisory output, separate from canonical decisions."""

    __tablename__ = "model_inference_evaluations"
    __table_args__ = (
        UniqueConstraint("evaluation_key", name="uq_model_inference_evaluation_key"),
        Index("ix_model_inference_session_time", "forward_session_id", "evaluated_at"),
        CheckConstraint("execution_allowed = false", name="ck_model_inference_advisory_only"),
        CheckConstraint(
            "advisory_classification IN ('SUPPORTIVE', 'CAUTION', 'OBSERVATION_ONLY')",
            name="ck_model_inference_advisory_classification",
        ),
    )

    evaluation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    inference_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source_intelligence_record_id: Mapped[str] = mapped_column(
        ForeignKey("strategy_intelligence_records.id"), nullable=False, index=True
    )
    source_candidate_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    forward_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("forward_validation_sessions.id")
    )
    forward_signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("forward_validation_signals.id"), index=True
    )
    pair_zone_event_id: Mapped[str | None] = mapped_column(String(100), index=True)
    strategy_config_hash: Mapped[str | None] = mapped_column(String(64))
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    advisory_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
