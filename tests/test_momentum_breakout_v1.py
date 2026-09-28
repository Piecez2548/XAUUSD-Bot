from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from config.settings import Settings
from domain.events import DomainEvent, EventType, SystemStatusPayload
from models.market import AccountState, Candle, MarketSnapshot, SymbolSpecification, Tick, Timeframe
from models.observatory import RiskSnapshot
from notifications.templates import format_telegram_event
from persistence.database import Database
from persistence.orm import ForwardValidationSessionRecord
from services.forward_shadow import ForwardShadowWorker
from services.momentum_breakout import MOMENTUM_SETUP_TYPE, MomentumBreakoutV1

START = datetime(2026, 1, 1, tzinfo=UTC)


def _candle(timestamp: datetime, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp=timestamp,
        raw_timestamp=int(timestamp.timestamp()),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=100,
        spread=20,
        real_volume=0,
    )


def _snapshot(
    *, trigger_close: float = 106.0, forming: bool = False, spread_points: int = 240
) -> MarketSnapshot:
    m15 = tuple(
        _candle(
            START - timedelta(hours=8) + timedelta(minutes=15 * index),
            100 + index * 0.2,
            100.35 + index * 0.2,
            99.85 + index * 0.2,
            100.2 + index * 0.2,
        )
        for index in range(30)
    )
    m5: list[Candle] = []
    for index in range(29):
        close = 100 + index * 0.15
        high = close + 0.2
        low = close - 0.2
        if index == 20:
            close, high, low = 104.0, 104.8, 103.7
        elif index == 21:
            close, high, low = 103.5, 103.7, 102.4
        elif index == 22:
            close, high, low = 103.6, 103.8, 103.3
        m5.append(_candle(START + timedelta(minutes=5 * index), close - 0.1, high, low, close))
    trigger_time = START + timedelta(minutes=5 * 29)
    m5.append(_candle(trigger_time, 105.0, max(trigger_close + 0.1, 105.2), 104.7, trigger_close))
    symbol = SymbolSpecification(
        name="XAUUSDm", bid=105.8, ask=106.0, spread=spread_points, digits=2, point=0.01,
        trade_tick_size=0.01, trade_tick_value=1.0, trade_tick_value_profit=1.0,
        trade_tick_value_loss=1.0, contract_size=100, volume_min=0.01,
        volume_max=100, volume_step=0.01, trade_mode=4, trade_mode_name="full",
    )
    tick = Tick(
        timestamp=START + timedelta(minutes=5 * 29, seconds=30),
        raw_timestamp=int((START + timedelta(minutes=5 * 29, seconds=30)).timestamp()),
        bid=105.8, ask=106.0, last=105.8, volume=1, flags=0,
    )
    account = AccountState(
        balance=10_000, equity=10_000, margin=0, free_margin=10_000,
        margin_level=0, profit=0, leverage=100, currency="USD", server="Demo",
        trade_mode=0, trade_mode_name="demo",
    )
    return MarketSnapshot(
        account=account, symbol=symbol, tick=tick, positions=(),
        candles={Timeframe.M5: tuple(m5), Timeframe.M15: m15,
                 Timeframe.H1: (m15[-1],), Timeframe.H4: (m15[-1],)},
        generated_at=tick.timestamp,
    )


def _risk() -> RiskSnapshot:
    return RiskSnapshot(
        timestamp=START, equity=10_000, balance=10_000, open_risk_percent=0,
        open_risk_amount=0, remaining_risk_percent=6, risk_per_position=(),
        max_trade_risk_percent=2, max_aggregate_risk_percent=6,
        open_positions_count=0, unbounded_positions_count=0, margin_usage_percent=0,
        free_margin=10_000,
    )


def _strategy() -> MomentumBreakoutV1:
    return MomentumBreakoutV1(Settings(shadow_max_spread_points=100))


def test_valid_buy_breakout_has_structural_sl_and_two_r_target() -> None:
    decision = _strategy().evaluate(
        _snapshot(), risk=_risk(), candles_are_closed=True
    )
    assert decision.decision.value == "BUY"
    assert decision.strategy_name == MOMENTUM_SETUP_TYPE
    assert decision.stop_loss < decision.entry_price < decision.take_profit
    assert round(
        (decision.take_profit - decision.entry_price)
        / (decision.entry_price - decision.stop_loss), 6
    ) == 2.0
    assert decision.feature_context["breakout_level"] == 104.8
    assert decision.feature_context["setup_event_id"].startswith("momentum-event-")


def test_valid_sell_breakout_is_symmetric() -> None:
    source = _snapshot()
    mirrored = {
        timeframe: tuple(
            candle.model_copy(
                update={
                    "open": 210 - candle.open,
                    "high": 210 - candle.low,
                    "low": 210 - candle.high,
                    "close": 210 - candle.close,
                }
            )
            for candle in candles
        )
        for timeframe, candles in source.candles.items()
    }
    snapshot = source.model_copy(
        update={
            "symbol": source.symbol.model_copy(update={"bid": 104.0, "ask": 104.2}),
            "tick": source.tick.model_copy(update={"bid": 104.0, "ask": 104.2, "last": 104.0}),
            "candles": mirrored,
        }
    )
    decision = _strategy().evaluate(snapshot, risk=_risk(), candles_are_closed=True)
    assert decision.decision.value == "SELL"
    assert decision.take_profit < decision.entry_price < decision.stop_loss
    assert round(
        (decision.entry_price - decision.take_profit)
        / (decision.stop_loss - decision.entry_price), 6
    ) == 2.0


def test_unfinished_candle_fails_closed() -> None:
    decision = _strategy().evaluate(_snapshot(), risk=_risk(), candles_are_closed=False)
    assert decision.decision.value == "NO_TRADE"
    assert decision.reason_codes == ("CANDLE_INCOMPLETE",)


def test_close_that_does_not_clear_swing_fails_closed() -> None:
    decision = _strategy().evaluate(
        _snapshot(trigger_close=104.7), risk=_risk(), candles_are_closed=True
    )
    assert decision.decision.value == "NO_TRADE"
    assert decision.reason_codes == ("BREAKOUT_CLOSE_MISSING",)


def test_stale_data_fails_closed() -> None:
    decision = _strategy().evaluate(
        _snapshot(), risk=_risk(), data_freshness="STALE", candles_are_closed=True
    )
    assert decision.decision.value == "NO_TRADE"
    assert decision.reason_codes == ("DATA_STALE",)


def test_normal_xauusdm_spread_passes_momentum_gate() -> None:
    decision = _strategy().evaluate(
        _snapshot(spread_points=240), risk=_risk(), candles_are_closed=True
    )
    assert decision.decision.value == "BUY"


def test_momentum_spread_ceiling_boundary_is_deterministic() -> None:
    at_ceiling = _strategy().evaluate(
        _snapshot(spread_points=300), risk=_risk(), candles_are_closed=True
    )
    above_ceiling = _strategy().evaluate(
        _snapshot(spread_points=301), risk=_risk(), candles_are_closed=True
    )
    assert at_ceiling.decision.value == "BUY"
    assert above_ceiling.decision.value == "NO_TRADE"
    assert above_ceiling.reason_codes == ("SPREAD_TOO_HIGH",)


def test_future_m15_context_fails_closed() -> None:
    source = _snapshot()
    future_m15 = tuple(
        candle.model_copy(update={"timestamp": candle.timestamp + timedelta(hours=4)})
        for candle in source.candles[Timeframe.M15]
    )
    snapshot = source.model_copy(
        update={"candles": {**source.candles, Timeframe.M15: future_m15}}
    )
    decision = _strategy().evaluate(snapshot, risk=_risk(), candles_are_closed=True)
    assert decision.decision.value == "NO_TRADE"
    assert decision.reason_codes == ("M15_CONTEXT_INVALID",)


def test_momentum_signal_persistence_is_idempotent_and_provenance_complete(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'momentum.db').as_posix()}")
    database.create_schema()
    with database.session() as session:
        forward_session = ForwardValidationSessionRecord(
            session_id="forward-momentum-persistence",
            strategy_id="momentum_breakout_v1",
            strategy_version="1.0.0",
            strategy_config_hash="a" * 64,
            started_at=START - timedelta(hours=1),
            source_identity="test",
            symbol="XAUUSDm",
            timeframes_json=["M5", "M15"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        )
        session.add(forward_session)
        session.flush()
    worker = ForwardShadowWorker(
        Settings(database_url="sqlite:///:memory:"),
        database,
        logger=logging.getLogger("momentum-persistence-test"),
    )
    worker.session = forward_session
    decision = _strategy().evaluate(_snapshot(), risk=_risk(), candles_are_closed=True)
    first = worker._persist_signal(decision, _snapshot(), setup_type=MOMENTUM_SETUP_TYPE)
    second = worker._persist_signal(decision, _snapshot(), setup_type=MOMENTUM_SETUP_TYPE)
    later = worker._persist_signal(
        decision.model_copy(
            update={
                "m5_candle_timestamp": decision.m5_candle_timestamp
                + timedelta(minutes=5)
            }
        ),
        _snapshot(),
        setup_type=MOMENTUM_SETUP_TYPE,
    )
    assert first is not None and second is not None and later is not None
    assert first.id == second.id == later.id
    assert first.setup_type == MOMENTUM_SETUP_TYPE
    assert first.zone_id is None
    assert first.setup_event_id == decision.feature_context["setup_event_id"]
    assert first.setup_provenance_json["m5_trigger_candle"]["timestamp"]
    assert first.rr == 2.0
    database.dispose()


def test_momentum_telegram_signal_format_includes_trade_provenance() -> None:
    event = DomainEvent(
        event_type=EventType.MOMENTUM_SIGNAL,
        source="test",
        payload=SystemStatusPayload(
            message="Momentum signal",
            component="test",
            status="MOMENTUM_SIGNAL",
            diagnostics={
                "symbol": "XAUUSDm",
                "direction": "BUY",
                "setup_type": MOMENTUM_SETUP_TYPE,
                "entry": 2000.2,
                "stop_loss": 1990.0,
                "take_profit": 2020.6,
                "planned_rr": 2.0,
                "breakout_level": 1999.0,
                "m5_candle_timestamp": START,
            },
        ),
    )
    message = format_telegram_event(event)
    assert "🚀 MOMENTUM SIGNAL" in message
    assert "MOMENTUM_BREAKOUT_V1" in message
    assert "Breakout level: 1999.0" in message
