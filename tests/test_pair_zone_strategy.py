from datetime import UTC, datetime, timedelta

from config.settings import Settings
from models.market import (
    AccountState,
    Candle,
    MarketSnapshot,
    SymbolSpecification,
    Tick,
    Timeframe,
)
from models.observatory import RiskSnapshot
from models.shadow import ShadowAction
from services.pair_zone_strategy import PairZoneV1


def _candle(timestamp, open_price, high, low, close):
    return Candle(
        timestamp=timestamp, raw_timestamp=int(timestamp.timestamp()), open=open_price,
        high=high, low=low, close=close, tick_volume=100, spread=20, real_volume=0,
    )


def _risk():
    return RiskSnapshot(
        equity=10_000, balance=10_000, open_risk_percent=0, open_risk_amount=0,
        remaining_risk_percent=6, risk_per_position=tuple(), max_trade_risk_percent=2,
        max_aggregate_risk_percent=6, open_positions_count=0, unbounded_positions_count=0,
        free_margin=10_000,
    )


def _snapshot(timestamp, m5):
    start = datetime(2025, 12, 31, 4, tzinfo=UTC)
    h1 = tuple(
        _candle(start + timedelta(hours=index), 100 + index - 0.2, 100 + index + 1,
                100 + index - 1, 100 + index)
        for index in range(21)
    )
    h4 = (_candle(start, 100, 122, 98, 120),)
    m15 = (
        _candle(datetime(2026, 1, 1, 0, tzinfo=UTC), 100, 110, 95, 105),
        _candle(datetime(2026, 1, 1, 0, 15, tzinfo=UTC), 103, 120, 100, 115),
    )
    account = AccountState(
        balance=10_000, equity=10_000, margin=0, free_margin=10_000, margin_level=0,
        profit=0, leverage=100, currency="USD", server="research", trade_mode=0,
        trade_mode_name="research",
    )
    symbol = SymbolSpecification(
        name="XAUUSDm", bid=m5[-1].close, ask=m5[-1].close, spread=20, digits=2,
        point=0.01, trade_tick_size=0.01, trade_tick_value=1, trade_tick_value_profit=1,
        trade_tick_value_loss=1, contract_size=100, volume_min=0.01, volume_max=100,
        volume_step=0.01, trade_mode=4, trade_mode_name="research",
    )
    return MarketSnapshot(
        account=account, symbol=symbol,
        tick=Tick(timestamp=timestamp, raw_timestamp=int(timestamp.timestamp()), bid=m5[-1].close,
                  ask=m5[-1].close, last=m5[-1].close, volume=0, flags=0),
        positions=tuple(), candles={Timeframe.H4: h4, Timeframe.H1: h1,
                                     Timeframe.M15: m15, Timeframe.M5: tuple(m5)},
        generated_at=timestamp,
    )


def test_pair_detection_boundaries_and_creation_after_second_close():
    strategy = PairZoneV1(Settings())
    first = _candle(datetime(2026, 1, 1, 0, tzinfo=UTC), 100, 110, 95, 105)
    second = _candle(datetime(2026, 1, 1, 0, 15, tzinfo=UTC), 103, 120, 100, 115)
    zone = strategy.detect_pair(first, second)
    assert zone is not None
    assert zone.lower_bound == 100
    assert zone.upper_bound == 110
    assert zone.created_at == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    later = _candle(second.timestamp + timedelta(minutes=30), 103, 120, 100, 115)
    assert strategy.detect_pair(first, later) is None


def test_pair_zone_has_no_lookahead_and_confirms_once():
    strategy = PairZoneV1(Settings())
    before_close = _candle(datetime(2026, 1, 1, 0, 25, tzinfo=UTC), 108, 110, 107, 109)
    confirmation = _candle(datetime(2026, 1, 1, 0, 35, tzinfo=UTC), 109, 114, 106, 112)
    before = strategy.evaluate(
        _snapshot(before_close.timestamp, [before_close]), risk=_risk(), candles_are_closed=True
    )
    assert before.decision == ShadowAction.NO_TRADE
    assert before.reason_codes == ("NO_VALID_ZONE",)
    after = strategy.evaluate(
        _snapshot(confirmation.timestamp, [before_close, confirmation]),
        risk=_risk(), candles_are_closed=True,
    )
    assert after.decision == ShadowAction.BUY
    assert after.entry_price == 112
    assert after.stop_loss == 99.5
    assert after.feature_context["zone_state"] == "CONFIRMED"
    repeated = strategy.evaluate(
        _snapshot(confirmation.timestamp, [before_close, confirmation]),
        risk=_risk(), candles_are_closed=True,
    )
    assert repeated.decision == ShadowAction.NO_TRADE


def test_pair_zone_touch_invalidation_and_expiration_are_deterministic():
    strategy = PairZoneV1(Settings())
    first = _candle(datetime(2026, 1, 1, 0, tzinfo=UTC), 100, 110, 95, 105)
    second = _candle(datetime(2026, 1, 1, 0, 15, tzinfo=UTC), 103, 120, 100, 115)
    zone = strategy.detect_pair(first, second)
    assert zone is not None
    touch = _candle(datetime(2026, 1, 1, 0, 35, tzinfo=UTC), 108, 110, 107, 109)
    state, touches, _ = strategy._lifecycle(zone, (touch,), touch.timestamp)
    assert state == "TOUCHED"
    assert touches == 1
    invalid = _candle(datetime(2026, 1, 1, 0, 40, tzinfo=UTC), 109, 110, 98, 99)
    state, _, _ = strategy._lifecycle(zone, (touch, invalid), invalid.timestamp)
    assert state == "INVALIDATED"
    state, _, _ = strategy._lifecycle(zone, tuple(), zone.created_at + timedelta(minutes=361))
    assert state == "EXPIRED"
