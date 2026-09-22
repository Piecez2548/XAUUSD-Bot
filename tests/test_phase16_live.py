from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from config.settings import Settings
from domain.events import EventType
from events.bus import EventBus
from models.history import DealFact
from models.live import FreshnessState
from models.market import Timeframe
from notifications.telegram import TelegramNotifier
from persistence.database import Database
from persistence.orm import BrokerDealRecord
from persistence.repositories import HistoryRepository, SnapshotRepository
from services.live import LiveDataEngine, _freshness
from services.reconstruction import reconstruct_closed_positions
from services.risk import (
    RISK_PERCENT_TOLERANCE,
    calculate_risk_snapshot,
    normalize_risk_state,
    risk_state_changed,
)


@pytest.mark.asyncio
async def test_candle_worker_refreshes_atomic_closed_candle_state(
    tmp_path: Path, model_parts, monkeypatch
) -> None:
    _account, symbol, _tick, candles, _position = model_parts
    database = Database(f"sqlite:///{(tmp_path / 'candle-state.db').as_posix()}")
    database.create_schema()

    class FakeGateway:
        async def call(self, operation):
            return operation(None)

    monkeypatch.setattr(
        "services.live.read_completed_candles",
        lambda _api, _symbol, timeframe, _count: candles[timeframe],
    )
    engine = LiveDataEngine(
        Settings(), database, EventBus(logging.getLogger("candle-state")),
        logger=logging.getLogger("candle-state"), gateway=FakeGateway(),
    )
    engine.state.symbol = symbol.name
    engine.state.specification = symbol
    await engine._candle_loop()
    assert set(engine.state.candles) == set(Timeframe)
    assert all(engine.state.candles[timeframe] == candles[timeframe] for timeframe in Timeframe)
    database.dispose()


def test_live_settings_allow_zero_websocket_throttle(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_WEBSOCKET_TICK_THROTTLE_MS", "0")
    assert Settings(websocket_tick_throttle_ms=0).websocket_tick_throttle_ms == 0


def test_telegram_routing_separates_telemetry_from_operator_alerts() -> None:
    assert EventType.HISTORY_SYNC_COMPLETED not in TelegramNotifier.NOTIFIABLE_EVENT_TYPES
    assert EventType.ACCOUNT_UPDATED not in TelegramNotifier.NOTIFIABLE_EVENT_TYPES
    assert EventType.CANDLE_CLOSED not in TelegramNotifier.NOTIFIABLE_EVENT_TYPES
    assert EventType.POSITION_OPENED in TelegramNotifier.NOTIFIABLE_EVENT_TYPES
    assert EventType.HISTORY_SYNC_FAILED in TelegramNotifier.NOTIFIABLE_EVENT_TYPES


def test_freshness_transitions_live_and_stale() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    live = _freshness(now - timedelta(seconds=1), now, 10, now)
    stale = _freshness(now - timedelta(seconds=11), now, 10, now)
    unknown = _freshness(None, None, 10, now)
    assert live.state is FreshnessState.LIVE
    assert stale.state is FreshnessState.STALE
    assert unknown.state is FreshnessState.UNKNOWN


def test_history_cursor_and_deal_persistence_are_idempotent(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'live.db').as_posix()}")
    database.create_schema()
    try:
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        facts = (
            DealFact(
                deal_ticket=10,
                position_id=99,
                symbol="XAUUSD",
                timestamp=timestamp,
                entry_type="0",
                volume=0.1,
                price=2000,
                profit=0,
            ),
        )
        repository = HistoryRepository(database)
        assert repository.persist_deals(facts, scope="deals:XAUUSD") == 1
        assert repository.persist_deals(facts, scope="deals:XAUUSD") == 0
        assert repository.cursor("deals:XAUUSD") == (timestamp, 10)
        with database.session() as session:
            assert session.query(BrokerDealRecord).count() == 1
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_history_telemetry_is_silent(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'history-live.db').as_posix()}")
    database.create_schema()
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, request=request)

    class FakeHistory:
        def cursor(self, _scope):
            return None

        def persist_deals(self, _facts, *, scope):
            return 0

    class FakeGateway:
        fail = False

        async def call(self, operation):
            if self.fail:
                raise RuntimeError("history unavailable")
            return ()

    try:
        bus = EventBus(logging.getLogger("history-test"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            notifier = TelegramNotifier(
                enabled=True,
                bot_token="secret",
                chat_id="chat",
                client=client,
                max_attempts=1,
            )
            bus.subscribe("telegram", notifier.handle)
            engine = LiveDataEngine(Settings(), database, bus, logger=logging.getLogger("engine"))
            engine.state.symbol = "XAUUSD"
            gateway = FakeGateway()
            engine.gateway = gateway
            engine.history = FakeHistory()
            await engine._history_loop()
            await engine._history_loop()
            assert attempts == 0
            assert engine.state.last_history_sync is not None
            assert engine._history_health_state == "HEALTHY"
            gateway.fail = True
            with pytest.raises(RuntimeError):
                await engine._history_loop()
            with pytest.raises(RuntimeError):
                await engine._history_loop()
            assert attempts == 1
            gateway.fail = False
            await engine._history_loop()
            assert attempts == 2
    finally:
        database.dispose()


def test_reconstruction_requires_verified_entry_and_exit() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    entry = DealFact(
        deal_ticket=1,
        position_id=7,
        symbol="XAUUSD",
        timestamp=timestamp,
        entry_type="0",
        volume=0.1,
        price=2000,
    )
    exit_fact = entry.model_copy(
        update={
            "deal_ticket": 2,
            "timestamp": timestamp + timedelta(minutes=5),
            "entry_type": "1",
            "price": 2005,
            "profit": 50,
        }
    )
    assert len(reconstruct_closed_positions((entry,))) == 0
    trades = reconstruct_closed_positions((entry, exit_fact))
    assert len(trades) == 1
    assert trades[0].deal_tickets == (1, 2)


def test_live_status_api_is_sanitized(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    database.create_schema()
    try:
        app = create_app(
            settings=Settings(
                telegram_enabled=True,
                telegram_bot_token="secret-token",
                telegram_chat_id="private-chat",
            ),
            database=database,
        )
        with TestClient(app) as client:
            response = client.get("/api/live/status")
        assert response.status_code == 200
        body = response.json()
        assert body["read_only"] is True
        assert "secret-token" not in response.text
        assert "private-chat" not in response.text
    finally:
        database.dispose()


def test_risk_initial_and_unchanged_cycles_are_distinct(model_parts) -> None:
    account, symbol, tick, candles, position = model_parts
    from models.market import MarketSnapshot

    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(),
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    first = calculate_risk_snapshot(
        snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
    )
    second = first.model_copy(update={"timestamp": first.timestamp + timedelta(seconds=5)})
    assert risk_state_changed(None, first)
    assert not risk_state_changed(normalize_risk_state(first), second)


def test_risk_changes_for_position_lifecycle_and_stop_state(model_parts) -> None:
    account, symbol, tick, candles, position = model_parts
    from models.market import MarketSnapshot

    empty = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(),
        generated_at=datetime.now(UTC),
    )
    opened = empty.model_copy(update={"positions": (position,)})
    bounded = calculate_risk_snapshot(empty, max_trade_risk_percent=2, max_aggregate_risk_percent=6)
    opened_risk = calculate_risk_snapshot(
        opened, max_trade_risk_percent=2, max_aggregate_risk_percent=6
    )
    assert risk_state_changed(normalize_risk_state(bounded), opened_risk)
    unbounded = opened.model_copy(
        update={"positions": (position.model_copy(update={"stop_loss": 0}),)}
    )
    unbounded_risk = calculate_risk_snapshot(
        unbounded, max_trade_risk_percent=2, max_aggregate_risk_percent=6
    )
    assert risk_state_changed(normalize_risk_state(opened_risk), unbounded_risk)
    assert risk_state_changed(normalize_risk_state(unbounded_risk), bounded)


def test_risk_float_tolerance_and_limit_crossing(model_parts) -> None:
    account, symbol, tick, candles, _position = model_parts
    from models.market import MarketSnapshot

    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(),
        generated_at=datetime.now(UTC),
    )
    base = calculate_risk_snapshot(snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6)
    tiny = base.model_copy(
        update={
            "open_risk_percent": (base.open_risk_percent or 0) + RISK_PERCENT_TOLERANCE / 2,
            "remaining_risk_percent": (base.remaining_risk_percent or 0)
            - RISK_PERCENT_TOLERANCE / 2,
        }
    )
    assert not risk_state_changed(normalize_risk_state(base), tiny)
    crossed = base.model_copy(update={"open_risk_percent": 6.01, "remaining_risk_percent": -0.01})
    assert risk_state_changed(normalize_risk_state(base), crossed)


def test_unchanged_risk_is_silent_for_event_and_telegram_policy(model_parts) -> None:
    account, symbol, tick, candles, _position = model_parts
    from models.market import MarketSnapshot

    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(),
        generated_at=datetime.now(UTC),
    )
    first = calculate_risk_snapshot(
        snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
    )
    repeated = first.model_copy(update={"timestamp": first.timestamp + timedelta(seconds=5)})
    emitted = []
    if risk_state_changed(normalize_risk_state(first), repeated):
        emitted.append("RISK_SNAPSHOT_CREATED")
        emitted.append("TELEGRAM")
    assert emitted == []


def test_current_risk_api_remains_fresh_without_meaningful_event(
    tmp_path: Path, model_parts
) -> None:
    account, symbol, tick, candles, _position = model_parts
    from models.market import MarketSnapshot

    database = Database(f"sqlite:///{(tmp_path / 'risk-api.db').as_posix()}")
    database.create_schema()
    try:
        first_time = datetime(2026, 1, 1, tzinfo=UTC)
        snapshot = MarketSnapshot(
            account=account,
            symbol=symbol,
            tick=tick,
            candles=candles,
            positions=(),
            generated_at=first_time,
        )
        risk = calculate_risk_snapshot(
            snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
        )
        SnapshotRepository(database).persist_live_risk(risk)
        newer = risk.model_copy(update={"timestamp": first_time + timedelta(seconds=5)})
        SnapshotRepository(database).persist_live_risk(newer)
        with TestClient(create_app(settings=Settings(), database=database)) as client:
            response = client.get("/api/risk/current")
        assert response.status_code == 200
        assert response.json()["timestamp"].startswith("2026-01-01T00:00:05")
    finally:
        database.dispose()
