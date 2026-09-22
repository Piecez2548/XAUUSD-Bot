"""Read and validate historical market data without fabricating candles."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pandas as pd

from models.market import Candle, Timeframe

REQUIRED_COLUMNS = (
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)


class MarketDataError(RuntimeError):
    """Raised when MT5 data is unavailable or fails validation."""


def validate_ohlcv(
    frame: pd.DataFrame,
    *,
    requested_count: int,
    minimum_ratio: float = 0.8,
) -> None:
    """Validate one raw MT5 candle frame, failing closed on malformed input."""

    errors: list[str] = []
    if frame.empty:
        raise MarketDataError("candle dataframe is empty")

    missing_columns = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing_columns:
        raise MarketDataError(f"candle dataframe is missing columns: {missing_columns}")

    minimum_count = max(1, int(requested_count * minimum_ratio))
    if len(frame) < minimum_count:
        errors.append(
            f"received {len(frame)} candles; expected at least {minimum_count} "
            f"of {requested_count} requested"
        )
    if len(frame) > requested_count:
        errors.append(f"received {len(frame)} candles, more than the {requested_count} requested")

    timestamps = frame["time"]
    if timestamps.isna().any():
        errors.append("timestamps contain null values")
    if timestamps.duplicated().any():
        duplicates = timestamps[timestamps.duplicated()].tolist()[:5]
        errors.append(f"duplicate timestamps detected: {duplicates}")
    if not timestamps.is_monotonic_increasing:
        errors.append("timestamps are not ordered from oldest to newest")

    price_columns = ["open", "high", "low", "close"]
    numeric_prices = frame[price_columns].apply(pd.to_numeric, errors="coerce")
    if numeric_prices.isna().any().any():
        errors.append("OHLC values contain null or non-numeric data")
    if (~numeric_prices.map(lambda value: bool(pd.notna(value) and float(value) > 0))).any().any():
        errors.append("OHLC prices must be positive")
    if (numeric_prices["high"] < numeric_prices[["open", "close"]].max(axis=1)).any():
        errors.append("one or more candle highs are below open or close")
    if (numeric_prices["low"] > numeric_prices[["open", "close"]].min(axis=1)).any():
        errors.append("one or more candle lows are above open or close")
    if (numeric_prices["high"] < numeric_prices["low"]).any():
        errors.append("one or more candle highs are below candle lows")

    volume_columns = ["tick_volume", "spread", "real_volume"]
    numeric_volumes = frame[volume_columns].apply(pd.to_numeric, errors="coerce")
    if numeric_volumes.isna().any().any() or (numeric_volumes < 0).any().any():
        errors.append("volume and spread values must be non-negative numbers")

    if errors:
        raise MarketDataError("; ".join(errors))


def _to_candles(frame: pd.DataFrame) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            timestamp=row.timestamp.to_pydatetime(),
            raw_timestamp=int(row.time),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            tick_volume=int(row.tick_volume),
            spread=int(row.spread),
            real_volume=int(row.real_volume),
        )
        for row in frame.itertuples(index=False)
    )


def read_candles(
    api: Any,
    symbol: str,
    timeframe: Timeframe,
    count: int,
    *,
    minimum_ratio: float,
) -> tuple[Candle, ...]:
    timeframe_constant = getattr(api, f"TIMEFRAME_{timeframe.value}", None)
    if timeframe_constant is None:
        raise MarketDataError(f"MT5 does not expose TIMEFRAME_{timeframe.value}")
    rates = api.copy_rates_from_pos(symbol, timeframe_constant, 0, count)
    if rates is None:
        raise MarketDataError(
            f"MT5 copy_rates_from_pos failed for {symbol} {timeframe.value}: {api.last_error()!r}"
        )

    frame = pd.DataFrame(rates)
    if not frame.empty and "time" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    validate_ohlcv(frame, requested_count=count, minimum_ratio=minimum_ratio)
    return _to_candles(frame)


def read_all_candles(
    api: Any,
    symbol: str,
    counts: Mapping[str, int],
    *,
    minimum_ratio: float,
    logger: logging.Logger | None = None,
) -> dict[Timeframe, tuple[Candle, ...]]:
    result: dict[Timeframe, tuple[Candle, ...]] = {}
    log = logger or logging.getLogger(__name__)
    for timeframe in Timeframe:
        count = counts[timeframe.value]
        try:
            result[timeframe] = read_candles(
                api,
                symbol,
                timeframe,
                count,
                minimum_ratio=minimum_ratio,
            )
        except MarketDataError:
            log.exception("Market data validation/retrieval failed for %s", timeframe.value)
            raise
        log.info("Retrieved and validated %d %s candles", len(result[timeframe]), timeframe.value)
    return result


def read_completed_candles(
    api: Any,
    symbol: str,
    timeframe: Timeframe,
    count: int,
    *,
    minimum_ratio: float = 0.8,
) -> tuple[Candle, ...]:
    """Read only closed bars, excluding the currently forming bar at position 0."""

    timeframe_constant = getattr(api, f"TIMEFRAME_{timeframe.value}", None)
    if timeframe_constant is None:
        raise MarketDataError(f"MT5 does not expose TIMEFRAME_{timeframe.value}")
    rates = api.copy_rates_from_pos(symbol, timeframe_constant, 1, count)
    if rates is None:
        raise MarketDataError(
            "MT5 completed-candle query failed for "
            f"{symbol} {timeframe.value}: {api.last_error()!r}"
        )
    frame = pd.DataFrame(rates)
    if not frame.empty and "time" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    validate_ohlcv(frame, requested_count=count, minimum_ratio=minimum_ratio)
    return _to_candles(frame)


def read_completed_candles_paginated(
    api: Any,
    symbol: str,
    timeframe: Timeframe,
    count: int,
    *,
    chunk_size: int = 5_000,
) -> tuple[Candle, ...]:
    """Read a bounded historical range in closed-bar chunks.

    MT5 rejects very large ``copy_rates_from_pos`` requests on some terminals.
    Requests therefore stay bounded and walk backwards by position.  Returned
    chunks are normalized, validated, deduplicated by timestamp, and sorted
    oldest-to-newest.  No missing interval is filled.
    """
    if count <= 0:
        raise MarketDataError("historical candle count must be greater than zero")
    if chunk_size <= 0 or chunk_size > 50_000:
        raise MarketDataError("historical chunk size must be between 1 and 50000")
    timeframe_constant = getattr(api, f"TIMEFRAME_{timeframe.value}", None)
    if timeframe_constant is None:
        raise MarketDataError(f"MT5 does not expose TIMEFRAME_{timeframe.value}")

    by_timestamp: dict[int, Candle] = {}
    offset = 0
    while len(by_timestamp) < count:
        requested = min(chunk_size, count - len(by_timestamp))
        rates = api.copy_rates_from_pos(symbol, timeframe_constant, 1 + offset, requested)
        if rates is None:
            raise MarketDataError(
                f"MT5 paginated candle query failed for {symbol} {timeframe.value}: "
                f"{api.last_error()!r}"
            )
        frame = pd.DataFrame(rates)
        if frame.empty:
            break
        if "time" in frame.columns:
            frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        # A short final response is valid evidence that the terminal has no
        # older bars; validate the rows returned rather than inventing padding.
        validate_ohlcv(
            frame,
            requested_count=len(frame),
            minimum_ratio=1.0,
        )
        candles = _to_candles(frame)
        before = len(by_timestamp)
        for candle in candles:
            by_timestamp.setdefault(candle.raw_timestamp, candle)
        offset += len(candles)
        if len(candles) < requested or len(by_timestamp) == before:
            break
    return tuple(by_timestamp[key] for key in sorted(by_timestamp)[:count])
