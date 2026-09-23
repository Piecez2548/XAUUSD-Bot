"""Immutable Phase 3 strategy-intelligence domain models.

These models describe observable evidence and alert decisions only.  They do
not contain an execution method and every candidate is explicitly
``execution_allowed=False``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntelligenceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CandidateState(StrEnum):
    OBSERVING = "OBSERVING"
    APPROACHING = "APPROACHING"
    IN_ZONE = "IN_ZONE"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


class TrendDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    UNCLEAR = "UNCLEAR"


class EvidencePolarity(StrEnum):
    SUPPORTIVE = "SUPPORTIVE"
    NEUTRAL = "NEUTRAL"
    CONTRADICTORY = "CONTRADICTORY"
    BLOCKING = "BLOCKING"


class ConfidenceBand(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class AlertDecision(StrEnum):
    IGNORE = "IGNORE"
    OBSERVE = "OBSERVE"
    ALERT = "ALERT"
    BLOCK = "BLOCK"


class DataQualityStatus(StrEnum):
    READY = "READY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE = "STALE"
    INVALID = "INVALID"


class SwingPoint(IntelligenceModel):
    kind: str
    price: float
    pivot_timestamp: datetime
    confirmed_at: datetime
    left_bars: int
    right_bars: int


class MarketContext(IntelligenceModel):
    symbol: str
    as_of: datetime
    version: str
    data_status: DataQualityStatus
    data_reasons: tuple[str, ...] = ()
    trend: TrendDirection = TrendDirection.UNCLEAR
    m15_trend: TrendDirection = TrendDirection.UNCLEAR
    m5_trend: TrendDirection = TrendDirection.UNCLEAR
    m15_structure: str = "INSUFFICIENT"
    m5_structure: str = "INSUFFICIENT"
    m15_swing_points: tuple[SwingPoint, ...] = ()
    m5_swing_points: tuple[SwingPoint, ...] = ()
    m15_atr: float | None = None
    m5_atr: float | None = None
    m15_displacement: float | None = None
    m5_displacement: float | None = None
    m5_relative_volatility: float | None = None
    session: str = "UNKNOWN"
    latest_m15_timestamp: datetime | None = None
    latest_m5_timestamp: datetime | None = None


class EvidenceItem(IntelligenceModel):
    type: str
    source: str
    timeframe: str
    value: str | float | int | bool | None
    normalized_value: float = Field(ge=-1, le=1)
    polarity: EvidencePolarity
    strength: float = Field(ge=0, le=1)
    timestamp: datetime
    rule_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoreComponent(IntelligenceModel):
    category: str
    raw_evidence: tuple[str, ...]
    normalized_contribution: float
    weight: float = Field(ge=0, le=1)
    reason: str


class StrategyCandidate(IntelligenceModel):
    candidate_id: str
    symbol: str
    strategy: str
    strategy_version: str
    direction: Direction
    timeframe: str
    detected_at: datetime
    context_reference: str
    state: CandidateState
    evidence: tuple[EvidenceItem, ...] = ()
    score: float = Field(ge=0, le=100)
    confidence_band: ConfidenceBand
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    score_components: tuple[ScoreComponent, ...] = ()
    alert_decision: AlertDecision = AlertDecision.IGNORE
    source: str = "deterministic"
    evidence_version: str = "evidence_v1"
    execution_allowed: bool = False

    @model_validator(mode="after")
    def validate_safety(self) -> StrategyCandidate:
        if self.execution_allowed:
            raise ValueError("strategy intelligence can never enable execution")
        if self.state == CandidateState.INVALIDATED and not self.blockers:
            raise ValueError("invalidated candidates require a blocker")
        if self.alert_decision == AlertDecision.BLOCK and not self.blockers:
            raise ValueError("BLOCK requires a hard blocker")
        return self


class ManualOBObservation(IntelligenceModel):
    """Trader-supplied/manual zone observation; never an entry confirmation."""

    observation_id: str
    symbol: str
    direction: Direction
    state: str
    timeframe: str = "M15"
    observed_at: datetime
    zone_reference: str | None = None
    notes: str | None = None


class IntelligenceResult(IntelligenceModel):
    context: MarketContext
    candidate: StrategyCandidate

    def model_dump_json_safe(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
