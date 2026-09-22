from __future__ import annotations

from types import SimpleNamespace

import pytest

from mt5.account import read_account_state
from mt5.positions import PositionReadError, read_open_positions
from mt5.symbols import read_symbol_specification, read_tick


class FakeReadApi:
    @staticmethod
    def last_error():
        return (0, "ok")

    @staticmethod
    def account_info():
        return SimpleNamespace(
            balance=10_000,
            equity=10_050,
            margin=100,
            margin_free=9_950,
            margin_level=10_050,
            profit=50,
            leverage=100,
            currency="USD",
            server="Demo",
            trade_mode=0,
        )

    @staticmethod
    def symbol_info(symbol: str):
        return SimpleNamespace(
            name=symbol,
            bid=2000,
            ask=2000.2,
            spread=20,
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=1,
            trade_tick_value_profit=1,
            trade_tick_value_loss=1,
            trade_contract_size=100,
            volume_min=0.01,
            volume_max=100,
            volume_step=0.01,
            trade_mode=4,
        )

    @staticmethod
    def symbol_info_tick(symbol: str):
        assert symbol == "XAUUSD"
        return SimpleNamespace(
            time=1_700_000_000,
            bid=2000,
            ask=2000.2,
            last=2000.1,
            volume=2,
            flags=6,
        )

    @staticmethod
    def positions_get(*, symbol: str):
        return (
            SimpleNamespace(
                ticket=123,
                symbol=symbol,
                type=0,
                volume=0.1,
                price_open=1990,
                price_current=2000,
                sl=1980,
                tp=2020,
                profit=100,
                swap=-1,
                magic=0,
                comment="manual",
                time=1_700_000_000,
            ),
        )


def test_account_reader_maps_mt5_fields() -> None:
    account = read_account_state(FakeReadApi())
    assert account.free_margin == 9_950
    assert account.trade_mode_name == "demo"


def test_symbol_and_tick_readers_map_broker_values() -> None:
    specification = read_symbol_specification(FakeReadApi(), "XAUUSD")
    tick = read_tick(FakeReadApi(), "XAUUSD")
    assert specification.contract_size == 100
    assert specification.trade_mode_name == "full"
    assert tick.timestamp.utcoffset() is not None
    assert tick.raw_timestamp == 1_700_000_000


def test_position_reader_is_read_only_and_maps_all_fields() -> None:
    positions = read_open_positions(FakeReadApi(), "XAUUSD")
    assert len(positions) == 1
    assert positions[0].ticket == 123
    assert positions[0].type_name == "BUY"
    assert positions[0].stop_loss == 1980
    assert positions[0].take_profit == 2020
    assert positions[0].open_time.utcoffset() is not None


def test_successful_empty_positions_is_zero_open_positions() -> None:
    class EmptyPositionsApi(FakeReadApi):
        @staticmethod
        def positions_get(*, symbol: str):
            return ()

    assert read_open_positions(EmptyPositionsApi(), "XAUUSD") == ()


def test_position_reader_failure_is_not_interpreted_as_empty() -> None:
    class FailedPositionsApi(FakeReadApi):
        @staticmethod
        def positions_get(*, symbol: str):
            return None

    with pytest.raises(PositionReadError):
        read_open_positions(FailedPositionsApi(), "XAUUSD")
