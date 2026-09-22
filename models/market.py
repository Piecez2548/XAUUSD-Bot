"""Typed, validated read-only market snapshot models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Timeframe(StrEnum):
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AccountState(FrozenModel):
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    profit: float
    leverage: int = Field(ge=0)
    currency: str
    server: str
    trade_mode: int | None = None
    trade_mode_name: str | None = None


class Tick(FrozenModel):
    timestamp: datetime
    raw_timestamp: int = Field(ge=0)
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    last: float = Field(ge=0)
    volume: float = Field(ge=0)
    flags: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_tick(self) -> Tick:
        if self.timestamp.utcoffset() is None:
            raise ValueError("tick timestamp must be timezone-aware")
        if self.ask < self.bid:
            raise ValueError("tick ask must be greater than or equal to bid")
        return self


class SymbolSpecification(FrozenModel):
    name: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    spread: int = Field(ge=0)
    digits: int = Field(ge=0)
    point: float = Field(gt=0)
    trade_tick_size: float = Field(gt=0)
    trade_tick_value: float = Field(ge=0)
    trade_tick_value_profit: float = Field(ge=0)
    trade_tick_value_loss: float = Field(ge=0)
    contract_size: float = Field(gt=0)
    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    trade_mode: int
    trade_mode_name: str

    @model_validator(mode="after")
    def validate_specification(self) -> SymbolSpecification:
        if self.ask < self.bid:
            raise ValueError("symbol ask must be greater than or equal to bid")
        if self.volume_max < self.volume_min:
            raise ValueError("volume_max must be greater than or equal to volume_min")
        return self


class Candle(FrozenModel):
    timestamp: datetime
    raw_timestamp: int = Field(ge=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    tick_volume: int = Field(ge=0)
    spread: int = Field(ge=0)
    real_volume: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_candle(self) -> Candle:
        prices = (self.open, self.high, self.low, self.close)
        if not all(isfinite(price) for price in prices):
            raise ValueError("candle prices must be finite")
        if self.timestamp.utcoffset() is None:
            raise ValueError("candle timestamp must be timezone-aware")
        if self.high < max(self.open, self.close):
            raise ValueError("candle high cannot be below open or close")
        if self.low > min(self.open, self.close):
            raise ValueError("candle low cannot be above open or close")
        if self.high < self.low:
            raise ValueError("candle high cannot be below low")
        return self


class Position(FrozenModel):
    ticket: int = Field(gt=0)
    symbol: str
    type: int
    type_name: str
    volume: float = Field(gt=0)
    open_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    stop_loss: float = Field(ge=0)
    take_profit: float = Field(ge=0)
    profit: float
    swap: float
    magic_number: int
    comment: str
    open_time: datetime
    raw_open_time: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_open_time(self) -> Position:
        if self.open_time.utcoffset() is None:
            raise ValueError("position open_time must be timezone-aware")
        return self


class MarketSnapshot(FrozenModel):
    account: AccountState
    symbol: SymbolSpecification
    tick: Tick
    positions: tuple[Position, ...]
    candles: dict[Timeframe, tuple[Candle, ...]]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_snapshot(self) -> MarketSnapshot:
        if self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        expected = set(Timeframe)
        actual = set(self.candles)
        if actual != expected:
            missing = sorted(item.value for item in expected - actual)
            extra = sorted(str(item) for item in actual - expected)
            raise ValueError(
                f"snapshot candle timeframes mismatch; missing={missing}, extra={extra}"
            )
        if any(not series for series in self.candles.values()):
            raise ValueError("snapshot candle series cannot be empty")
        if any(position.symbol != self.symbol.name for position in self.positions):
            raise ValueError("all snapshot positions must match the selected symbol")
        return self
