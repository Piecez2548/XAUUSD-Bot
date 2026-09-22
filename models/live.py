"""Validated state contracts for the continuous read-only runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RuntimeState(StrEnum):
    STARTING = "STARTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class FreshnessState(StrEnum):
    LIVE = "LIVE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    MARKET_CLOSED = "MARKET_CLOSED"


class Freshness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: datetime | None = None
    source_timestamp: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    state: FreshnessState = FreshnessState.UNKNOWN


class WorkerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    state: RuntimeState
    last_attempted_at: datetime | None = None
    last_success_at: datetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    failure_count: int = Field(default=0, ge=0)
    last_error_category: str | None = None


class LiveStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: RuntimeState
    symbol: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    freshness: dict[str, Freshness] = Field(default_factory=dict)
    workers: tuple[WorkerHealth, ...] = ()
    history_cursor: datetime | None = None
    deals_synchronized: int = 0
