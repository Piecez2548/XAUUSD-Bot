"""Small, deterministic feature functions for the shadow engine."""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

from models.market import Candle
from models.shadow import FeatureSet


class FeatureValidationError(ValueError):
    """Raised when a candle window cannot be analysed safely."""


def closed_candles(candles: Sequence[Candle], *, forming_last: bool = True) -> tuple[Candle, ...]:
    """Return an ordered closed-only view without ever using a forming bar."""

    values = tuple(candles)
    if forming_last and values:
        values = values[:-1]
    if len(values) < 2:
        raise FeatureValidationError("insufficient closed candles")
    timestamps = [item.timestamp for item in values]
    if any(left >= right for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise FeatureValidationError("closed candles are not strictly chronological")
    for candle in values:
        if not all(
            isfinite(value) and value > 0
            for value in (candle.open, candle.high, candle.low, candle.close)
        ):
            raise FeatureValidationError("closed candle contains invalid OHLC")
    return values


def _ema(values: Sequence[float], period: int) -> float:
    alpha = 2 / (period + 1)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
    return current


def extract_features(
    candles: Sequence[Candle],
    *,
    timeframe: str,
    spread: float | None = None,
    forming_last: bool = True,
    atr_period: int = 14,
    ema_period: int = 20,
) -> FeatureSet:
    values = closed_candles(candles, forming_last=forming_last)
    latest = values[-1]
    true_ranges: list[float] = []
    for index, candle in enumerate(values):
        previous_close = values[index - 1].close if index else candle.open
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    atr_window = true_ranges[-min(atr_period, len(true_ranges)) :]
    atr = sum(atr_window) / len(atr_window) if atr_window else None
    closes = [item.close for item in values]
    ema = _ema(closes, min(ema_period, len(closes))) if closes else None
    previous_ema = _ema(closes[:-1], min(ema_period, len(closes) - 1)) if len(closes) > 2 else ema
    ema_slope = ema - previous_ema if ema is not None and previous_ema is not None else None
    range_width = latest.high - latest.low
    body_size = abs(latest.close - latest.open)
    upper_wick = latest.high - max(latest.open, latest.close)
    lower_wick = min(latest.open, latest.close) - latest.low
    body_to_range = body_size / range_width if range_width else 0.0
    returns = tuple((right / left) - 1 for left, right in zip(closes, closes[1:], strict=False))
    volatility_window = returns[-min(20, len(returns)) :]
    volatility = (
        sum(abs(value) for value in volatility_window) / len(volatility_window)
        if volatility_window
        else 0.0
    )
    long_window = returns[-min(100, len(returns)) :]
    baseline = sum(abs(value) for value in long_window) / len(long_window) if long_window else None
    relative_volatility = volatility / baseline if baseline and baseline > 0 else None
    swing_window = values[-min(5, len(values)) :]
    swing_high = max(item.high for item in swing_window)
    swing_low = min(item.low for item in swing_window)
    prior_window = values[-min(10, len(values)) : -min(5, len(values))]
    prior_high = max((item.high for item in prior_window), default=swing_high)
    prior_low = min((item.low for item in prior_window), default=swing_low)
    return FeatureSet(
        timeframe=timeframe,
        candle_timestamp=latest.timestamp,
        close=latest.close,
        returns=returns[-20:],
        true_range=true_ranges[-1],
        atr=atr,
        ema=ema,
        ema_slope=ema_slope,
        price_vs_ema=latest.close - ema if ema is not None else None,
        swing_high=swing_high,
        swing_low=swing_low,
        range_width=range_width,
        body_size=body_size,
        upper_wick=upper_wick,
        lower_wick=lower_wick,
        body_to_range=body_to_range,
        volatility=volatility,
        relative_volatility=relative_volatility,
        higher_high=swing_high > prior_high,
        higher_low=swing_low > prior_low,
        lower_high=swing_high < prior_high,
        lower_low=swing_low < prior_low,
        spread=spread,
    )
