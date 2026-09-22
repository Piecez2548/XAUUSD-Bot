"""Immutable models for the deterministic Phase 2 shadow decision boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ShadowAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class MarketRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


class ShadowModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class FeatureSet(ShadowModel):
    """Deterministic, serializable candle features used by the strategy."""

    timeframe: str
    candle_timestamp: datetime
    close: float
    returns: tuple[float, ...]
    true_range: float
    atr: float | None
    ema: float | None
    ema_slope: float | None
    price_vs_ema: float | None
    swing_high: float | None
    swing_low: float | None
    range_width: float
    body_size: float
    upper_wick: float
    lower_wick: float
    body_to_range: float
    volatility: float
    relative_volatility: float | None
    higher_high: bool
    higher_low: bool
    lower_high: bool
    lower_low: bool
    spread: float | None = None


class MarketContext(ShadowModel):
    """Multi-timeframe context. No broker/API references are allowed here."""

    regime: MarketRegime
    h4_bias: ShadowAction
    h1_bias: ShadowAction
    m15_setup: ShadowAction
    m5_timing: ShadowAction
    features: dict[str, FeatureSet]
    reason_codes: tuple[str, ...]
    version: str = "context_v1"


class RiskGateResult(ShadowModel):
    state: str
    approved: bool
    reason_codes: tuple[str, ...]
    current_open_risk_percent: float | None = None
    remaining_aggregate_risk_percent: float | None = None
    proposed_risk_percent: float | None = None


class ShadowDecision(ShadowModel):
    decision_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    market_snapshot_id: UUID | None = None
    symbol: str
    m5_candle_timestamp: datetime
    decision: ShadowAction
    market_regime: MarketRegime
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward_ratio: float | None = None
    requested_risk_percent: float | None = None
    approved_risk_percent: float | None = None
    hypothetical_volume: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    strategy_name: str = "baseline"
    strategy_version: str = "baseline_v1"
    reason_codes: tuple[str, ...] = ()
    human_readable_reason: str
    risk_gate_state: str
    data_freshness: str
    execution_allowed: bool = False
    feature_context: dict[str, object] = Field(default_factory=dict)
    outcome_status: str = "PENDING"

    @model_validator(mode="after")
    def validate_shadow_safety(self) -> ShadowDecision:
        if self.execution_allowed:
            raise ValueError("shadow decisions can never allow execution")
        if self.decision == ShadowAction.NO_TRADE and any(
            value is not None
            for value in (
                self.entry_price,
                self.stop_loss,
                self.take_profit,
                self.hypothetical_volume,
            )
        ):
            raise ValueError("NO_TRADE must not fabricate trade levels or volume")
        if self.decision in {ShadowAction.BUY, ShadowAction.SELL} and None in (
            self.entry_price,
            self.stop_loss,
            self.take_profit,
            self.hypothetical_volume,
        ):
            raise ValueError("trade decisions require entry, stop, target, and volume")
        return self
