"""Typed, versioned domain events shared by persistence and integrations."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventType(StrEnum):
    SYSTEM_STARTED = "SYSTEM_STARTED"
    SYSTEM_STOPPED = "SYSTEM_STOPPED"
    MT5_CONNECTED = "MT5_CONNECTED"
    MT5_DISCONNECTED = "MT5_DISCONNECTED"
    MT5_RECONNECTED = "MT5_RECONNECTED"
    MARKET_SNAPSHOT_CREATED = "MARKET_SNAPSHOT_CREATED"
    ACCOUNT_SNAPSHOT_CREATED = "ACCOUNT_SNAPSHOT_CREATED"
    RISK_SNAPSHOT_CREATED = "RISK_SNAPSHOT_CREATED"
    AI_DECISION_CREATED = "AI_DECISION_CREATED"
    SHADOW_DECISION_CREATED = "SHADOW_DECISION_CREATED"
    TRADE_REQUESTED = "TRADE_REQUESTED"
    TRADE_APPROVED = "TRADE_APPROVED"
    TRADE_REJECTED = "TRADE_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_ADDED = "POSITION_ADDED"
    POSITION_MODIFIED = "POSITION_MODIFIED"
    SL_MODIFIED = "SL_MODIFIED"
    TP_MODIFIED = "TP_MODIFIED"
    POSITION_PARTIALLY_CLOSED = "POSITION_PARTIALLY_CLOSED"
    POSITION_CLOSED = "POSITION_CLOSED"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    AI_CLOSE = "AI_CLOSE"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TELEGRAM_SENT = "TELEGRAM_SENT"
    RISK_LIMIT_REJECTED = "RISK_LIMIT_REJECTED"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    NEWS_SERVICE_UNAVAILABLE = "NEWS_SERVICE_UNAVAILABLE"
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    SYSTEM_LIVE_STARTED = "SYSTEM_LIVE_STARTED"
    SYSTEM_LIVE_STOPPED = "SYSTEM_LIVE_STOPPED"
    MT5_RECONNECTING = "MT5_RECONNECTING"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED"
    POSITION_OBSERVED_OPEN = "POSITION_OBSERVED_OPEN"
    POSITION_OBSERVED_CHANGED = "POSITION_OBSERVED_CHANGED"
    POSITION_NO_LONGER_OPEN = "POSITION_NO_LONGER_OPEN"
    HISTORY_SYNC_COMPLETED = "HISTORY_SYNC_COMPLETED"
    CANDLE_CLOSED = "CANDLE_CLOSED"
    DATA_STALE = "DATA_STALE"
    POSITION_WITHOUT_STOP_LOSS = "POSITION_WITHOUT_STOP_LOSS"
    RISK_UNBOUNDED = "RISK_UNBOUNDED"
    RISK_BACK_WITHIN_BOUNDS = "RISK_BACK_WITHIN_BOUNDS"
    HISTORY_SYNC_FAILED = "HISTORY_SYNC_FAILED"
    HISTORY_SYNC_RECOVERED = "HISTORY_SYNC_RECOVERED"


class EventSeverity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class PayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EmptyPayload(PayloadModel):
    kind: Literal["empty"] = "empty"


class SystemStatusPayload(PayloadModel):
    kind: Literal["system_status"] = "system_status"
    message: str
    component: str | None = None
    status: str | None = None
    diagnostics: dict[str, str | int | float] | None = None


class SnapshotCreatedPayload(PayloadModel):
    kind: Literal["snapshot_created"] = "snapshot_created"
    snapshot_id: UUID
    symbol: str
    positions_count: int = Field(ge=0)


class AccountSnapshotCreatedPayload(PayloadModel):
    kind: Literal["account_snapshot_created"] = "account_snapshot_created"
    account_snapshot_id: UUID
    currency: str


class RiskSnapshotCreatedPayload(PayloadModel):
    kind: Literal["risk_snapshot_created"] = "risk_snapshot_created"
    risk_snapshot_id: UUID
    open_risk_percent: float | None = Field(default=None, ge=0)
    remaining_risk_percent: float | None = None
    unbounded_positions: int = Field(ge=0)


class ErrorPayload(PayloadModel):
    kind: Literal["error"] = "error"
    error_code: str
    message: str
    recoverable: bool
    component: str


class TradeLifecyclePayload(PayloadModel):
    """Future-compatible audit payload; Phase 1.5 never executes these actions."""

    kind: Literal["trade_lifecycle"] = "trade_lifecycle"
    trade_id: UUID | None = None
    broker_ticket: int | None = None
    symbol: str
    direction: Literal["BUY", "SELL"] | None = None
    price: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, ge=0)
    take_profit: float | None = Field(default=None, ge=0)
    pnl: float | None = None
    risk_percent: float | None = Field(default=None, ge=0)
    risk_amount: float | None = Field(default=None, ge=0)
    planned_rr: float | None = None
    realized_r: float | None = None
    reason: str | None = None


class AiDecisionEventPayload(PayloadModel):
    kind: Literal["ai_decision"] = "ai_decision"
    decision_id: UUID
    symbol: str
    action: Literal["BUY", "SELL", "NO_TRADE", "WAIT", "HOLD", "MODIFY", "CLOSE"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    validation_status: str | None = None
    execution_status: str | None = None


class CandleClosedPayload(PayloadModel):
    kind: Literal["candle_closed"] = "candle_closed"
    symbol: str
    timeframe: Literal["M5", "M15", "H1", "H4"]
    open_time: datetime
    close_time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    tick_volume: int = Field(ge=0)
    spread: int = Field(ge=0)
    real_volume: int = Field(ge=0)


EventPayload = Annotated[
    EmptyPayload
    | SystemStatusPayload
    | SnapshotCreatedPayload
    | AccountSnapshotCreatedPayload
    | RiskSnapshotCreatedPayload
    | ErrorPayload
    | TradeLifecyclePayload
    | AiDecisionEventPayload
    | CandleClosedPayload,
    Field(discriminator="kind"),
]


class DomainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    event_type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = Field(min_length=1, max_length=100)
    severity: EventSeverity = EventSeverity.INFO
    correlation_id: UUID | None = None
    payload: EventPayload = Field(default_factory=EmptyPayload)
    schema_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_timestamp_and_payload(self) -> DomainEvent:
        if self.timestamp.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        payload_rules: dict[EventType, tuple[type[PayloadModel], ...]] = {
            EventType.MARKET_SNAPSHOT_CREATED: (SnapshotCreatedPayload,),
            EventType.ACCOUNT_SNAPSHOT_CREATED: (AccountSnapshotCreatedPayload,),
            EventType.RISK_SNAPSHOT_CREATED: (RiskSnapshotCreatedPayload,),
            EventType.SYSTEM_ERROR: (ErrorPayload,),
            EventType.AI_DECISION_CREATED: (AiDecisionEventPayload,),
            EventType.SHADOW_DECISION_CREATED: (AiDecisionEventPayload,),
            EventType.POSITION_OPENED: (TradeLifecyclePayload,),
            EventType.POSITION_ADDED: (TradeLifecyclePayload,),
            EventType.POSITION_MODIFIED: (TradeLifecyclePayload,),
            EventType.SL_MODIFIED: (TradeLifecyclePayload,),
            EventType.TP_MODIFIED: (TradeLifecyclePayload,),
            EventType.POSITION_PARTIALLY_CLOSED: (TradeLifecyclePayload,),
            EventType.POSITION_CLOSED: (TradeLifecyclePayload,),
            EventType.MANUAL_CLOSE: (TradeLifecyclePayload,),
            EventType.AI_CLOSE: (TradeLifecyclePayload,),
            EventType.TAKE_PROFIT: (TradeLifecyclePayload,),
            EventType.STOP_LOSS: (TradeLifecyclePayload,),
            EventType.RISK_LIMIT_REJECTED: (TradeLifecyclePayload,),
            EventType.CANDLE_CLOSED: (CandleClosedPayload,),
        }
        allowed = payload_rules.get(self.event_type)
        if allowed and not isinstance(self.payload, allowed):
            names = ", ".join(item.__name__ for item in allowed)
            raise ValueError(f"{self.event_type.value} requires payload type {names}")
        return self
