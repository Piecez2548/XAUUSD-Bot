from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from models.market import (
    AccountState,
    Candle,
    Position,
    SymbolSpecification,
    Tick,
    Timeframe,
)


@pytest.fixture(autouse=True)
def isolate_remote_dashboard_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local operator dotenv settings from changing unrelated test behavior."""

    monkeypatch.setenv("REMOTE_DASHBOARD_MODE", "false")


@pytest.fixture
def valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": [1_700_000_000, 1_700_000_300, 1_700_000_600],
            "open": [2000.0, 2001.0, 2002.0],
            "high": [2002.0, 2003.0, 2004.0],
            "low": [1999.0, 2000.0, 2001.0],
            "close": [2001.0, 2002.0, 2003.0],
            "tick_volume": [100, 110, 120],
            "spread": [20, 20, 21],
            "real_volume": [0, 0, 0],
        }
    )


@pytest.fixture
def symbol_factory():
    def factory(
        name: str,
        *,
        currency_base: str = "",
        currency_profit: str = "",
        description: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            currency_base=currency_base,
            currency_profit=currency_profit,
            description=description,
        )

    return factory


@pytest.fixture
def model_parts():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    account = AccountState(
        balance=10_000,
        equity=10_100,
        margin=100,
        free_margin=10_000,
        margin_level=10_100,
        profit=100,
        leverage=100,
        currency="USD",
        server="Test-Server",
        trade_mode=0,
        trade_mode_name="demo",
    )
    symbol = SymbolSpecification(
        name="XAUUSD",
        bid=2000,
        ask=2000.2,
        spread=20,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1,
        trade_tick_value_profit=1,
        trade_tick_value_loss=1,
        contract_size=100,
        volume_min=0.01,
        volume_max=100,
        volume_step=0.01,
        trade_mode=4,
        trade_mode_name="full",
    )
    tick = Tick(
        timestamp=start,
        raw_timestamp=int(start.timestamp()),
        bid=2000,
        ask=2000.2,
        last=2000.1,
        volume=1,
        flags=0,
    )
    candles = {
        timeframe: tuple(
            Candle(
                timestamp=start + timedelta(minutes=index * 5),
                raw_timestamp=int((start + timedelta(minutes=index * 5)).timestamp()),
                open=2000,
                high=2002,
                low=1999,
                close=2001,
                tick_volume=100,
                spread=20,
                real_volume=0,
            )
            for index in range(2)
        )
        for timeframe in Timeframe
    }
    position = Position(
        ticket=1,
        symbol="XAUUSD",
        type=0,
        type_name="BUY",
        volume=0.1,
        open_price=1990,
        current_price=2000,
        stop_loss=1980,
        take_profit=2020,
        profit=100,
        swap=0,
        magic_number=0,
        comment="manual",
        open_time=start,
        raw_open_time=int(start.timestamp()),
    )
    return account, symbol, tick, candles, position
