from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from models.market import Candle, MarketSnapshot, Timeframe


def test_valid_snapshot_is_immutable_and_complete(model_parts) -> None:
    account, symbol, tick, candles, position = model_parts
    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        positions=(position,),
        candles=candles,
    )
    assert set(snapshot.candles) == set(Timeframe)
    with pytest.raises(ValidationError):
        snapshot.account = account


def test_snapshot_rejects_missing_timeframe(model_parts) -> None:
    account, symbol, tick, candles, position = model_parts
    del candles[Timeframe.H4]
    with pytest.raises(ValidationError, match="timeframes mismatch"):
        MarketSnapshot(
            account=account,
            symbol=symbol,
            tick=tick,
            positions=(position,),
            candles=candles,
        )


def test_snapshot_rejects_position_for_another_symbol(model_parts) -> None:
    account, symbol, tick, candles, position = model_parts
    other = position.model_copy(update={"symbol": "EURUSD"})
    with pytest.raises(ValidationError, match="positions must match"):
        MarketSnapshot(
            account=account,
            symbol=symbol,
            tick=tick,
            positions=(other,),
            candles=candles,
        )


def test_candle_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Candle(
            timestamp=datetime(2026, 1, 1),
            raw_timestamp=1,
            open=1,
            high=2,
            low=0.5,
            close=1.5,
            tick_volume=1,
            spread=1,
            real_volume=0,
        )
