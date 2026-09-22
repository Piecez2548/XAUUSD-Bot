"""Read-only observatory models derived from Phase 1 market state."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObservatoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PositionRisk(ObservatoryModel):
    ticket: int
    risk_amount: float | None = Field(default=None, ge=0)
    risk_percent: float | None = Field(default=None, ge=0)
    bounded_by_stop: bool


class RiskSnapshot(ObservatoryModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    equity: float
    balance: float
    open_risk_percent: float | None = Field(default=None, ge=0)
    open_risk_amount: float | None = Field(default=None, ge=0)
    remaining_risk_percent: float | None = None
    risk_per_position: tuple[PositionRisk, ...]
    daily_pnl: float | None = None
    daily_realized_loss: float | None = None
    drawdown_percent: float | None = None
    max_trade_risk_percent: float = Field(gt=0)
    max_aggregate_risk_percent: float = Field(gt=0)
    open_positions_count: int = Field(ge=0)
    unbounded_positions_count: int = Field(ge=0)
    margin_usage_percent: float | None = Field(default=None, ge=0)
    free_margin: float


class PersistenceResult(ObservatoryModel):
    market_snapshot_id: UUID
    account_snapshot_id: UUID
    risk_snapshot_id: UUID
