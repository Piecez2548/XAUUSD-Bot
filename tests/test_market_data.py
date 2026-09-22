from __future__ import annotations

import pandas as pd
import pytest

from models.market import Timeframe
from mt5.market_data import (
    MarketDataError,
    read_candles,
    read_completed_candles_paginated,
    validate_ohlcv,
)


def test_valid_ohlcv_passes(valid_frame: pd.DataFrame) -> None:
    validate_ohlcv(valid_frame, requested_count=3, minimum_ratio=1)


def test_empty_frame_fails() -> None:
    with pytest.raises(MarketDataError, match="empty"):
        validate_ohlcv(pd.DataFrame(), requested_count=3)


def test_duplicate_timestamps_fail(valid_frame: pd.DataFrame) -> None:
    valid_frame.loc[2, "time"] = valid_frame.loc[1, "time"]
    with pytest.raises(MarketDataError, match="duplicate timestamps"):
        validate_ohlcv(valid_frame, requested_count=3)


def test_unordered_timestamps_fail(valid_frame: pd.DataFrame) -> None:
    valid_frame.loc[1, "time"] = valid_frame.loc[0, "time"] - 1
    with pytest.raises(MarketDataError, match="not ordered"):
        validate_ohlcv(valid_frame, requested_count=3)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("high", 1990.0, "highs are below"),
        ("low", 2010.0, "lows are above"),
        ("open", 0.0, "must be positive"),
    ],
)
def test_invalid_ohlc_fails(
    valid_frame: pd.DataFrame, column: str, value: float, message: str
) -> None:
    valid_frame.loc[0, column] = value
    with pytest.raises(MarketDataError, match=message):
        validate_ohlcv(valid_frame, requested_count=3)


def test_short_history_fails_reasonable_count_check(valid_frame: pd.DataFrame) -> None:
    with pytest.raises(MarketDataError, match="expected at least"):
        validate_ohlcv(valid_frame, requested_count=100, minimum_ratio=0.8)


def test_read_candles_returns_utc_models(valid_frame: pd.DataFrame) -> None:
    class FakeApi:
        TIMEFRAME_M5 = 5

        @staticmethod
        def copy_rates_from_pos(symbol: str, timeframe: int, start: int, count: int):
            assert (symbol, timeframe, start, count) == ("XAUUSD", 5, 0, 3)
            return valid_frame.to_records(index=False)

        @staticmethod
        def last_error():
            return (0, "ok")

    candles = read_candles(FakeApi(), "XAUUSD", Timeframe.M5, 3, minimum_ratio=1)
    assert len(candles) == 3
    assert candles[0].timestamp.utcoffset() is not None
    assert candles[0].raw_timestamp == 1_700_000_000


def test_paginated_closed_reader_orders_and_bounds_requests() -> None:
    frame = pd.DataFrame(
        {
            "time": [1_700_000_000 + index * 300 for index in range(5)],
            "open": [2000.0 + index for index in range(5)],
            "high": [2002.0 + index for index in range(5)],
            "low": [1999.0 + index for index in range(5)],
            "close": [2001.0 + index for index in range(5)],
            "tick_volume": [100] * 5,
            "spread": [20] * 5,
            "real_volume": [0] * 5,
        }
    )

    class FakeApi:
        TIMEFRAME_M5 = 5

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def copy_rates_from_pos(self, _symbol: str, _timeframe: int, start: int, count: int):
            self.calls.append((start, count))
            end = len(frame) - start + 1
            begin = max(0, end - count)
            return frame.iloc[begin:end].to_records(index=False)

        def last_error(self):
            return (0, "")

    api = FakeApi()
    candles = read_completed_candles_paginated(api, "XAUUSD", Timeframe.M5, 5, chunk_size=2)
    assert len(candles) == 5
    assert [item.raw_timestamp for item in candles] == sorted(
        item.raw_timestamp for item in candles
    )
    assert api.calls == [(1, 2), (3, 2), (5, 1)]
