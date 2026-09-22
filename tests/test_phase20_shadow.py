from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.app import create_app
from api.realtime import RealtimeHub
from config.settings import Settings
from events.bus import EventBus
from models.market import Candle, MarketSnapshot, Timeframe
from models.shadow import ShadowAction
from persistence.database import Database
from persistence.orm import ShadowDecisionRecord, SystemHealthRecord
from persistence.repositories import ShadowDecisionRepository, SystemHealthRepository
from services.control import TelegramControlService
from services.features import FeatureValidationError, extract_features
from services.risk import calculate_risk_snapshot
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_replay import replay_snapshots
from services.shadow_service import ShadowDecisionWorker, ShadowInput


@pytest.fixture
def shadow_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'shadow.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


def _trend_snapshot(model_parts, direction: str = "up") -> MarketSnapshot:
    account, symbol, tick, _candles, _position = model_parts
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rising = direction == "up"
    candles = {}
    for timeframe in Timeframe:
        values = []
        for index in range(31):
            base = 2000 + index if rising else 2000 - index
            open_price = float(base)
            close = open_price + (1 if rising else -1)
            high = max(open_price, close) + 1
            low = min(open_price, close) - 1
            values.append(
                Candle(
                    timestamp=start + timedelta(minutes=index * 5),
                    raw_timestamp=int((start + timedelta(minutes=index * 5)).timestamp()),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    tick_volume=100,
                    spread=20,
                    real_volume=0,
                )
            )
        candles[timeframe] = tuple(values)
    latest_close = candles[Timeframe.M5][-2].close
    adjusted_symbol = symbol.model_copy(
        update={"bid": latest_close - 0.1, "ask": latest_close + 0.1}
    )
    adjusted_tick = tick.model_copy(update={"bid": adjusted_symbol.bid, "ask": adjusted_symbol.ask})
    return MarketSnapshot(
        account=account,
        symbol=adjusted_symbol,
        tick=adjusted_tick,
        candles=candles,
        positions=(),
        generated_at=start + timedelta(minutes=200),
    )


def _engine_risk(snapshot: MarketSnapshot):
    return calculate_risk_snapshot(snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6)


def test_bullish_and_bearish_contexts_are_deterministic(model_parts) -> None:
    engine = ShadowDecisionEngine()
    bullish = engine.evaluate(
        _trend_snapshot(model_parts), risk=_engine_risk(_trend_snapshot(model_parts))
    )
    bearish_snapshot = _trend_snapshot(model_parts, "down")
    bearish = engine.evaluate(bearish_snapshot, risk=_engine_risk(bearish_snapshot))
    assert bullish.decision == ShadowAction.BUY
    assert bearish.decision == ShadowAction.SELL
    assert bullish.execution_allowed is False and bearish.execution_allowed is False


def test_conflicting_context_is_uncertain_no_trade(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    # Make only H1 bearish; the immutable model validator remains satisfied.
    bearish = _trend_snapshot(model_parts, "down").candles[Timeframe.H1]
    candles = dict(snapshot.candles)
    candles[Timeframe.H1] = bearish
    conflicted = snapshot.model_copy(update={"candles": candles})
    decision = ShadowDecisionEngine().evaluate(conflicted, risk=_engine_risk(conflicted))
    assert decision.decision == ShadowAction.NO_TRADE
    assert decision.reason_codes[0] in {"TREND_NOT_ALIGNED", "MARKET_REGIME_UNCERTAIN"}


def test_forming_candle_is_excluded(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    extreme = snapshot.candles[Timeframe.M5][-1].model_copy(update={"close": 100.0, "open": 100.0})
    changed = snapshot.model_copy(
        update={
            "candles": {
                **snapshot.candles,
                Timeframe.M5: (*snapshot.candles[Timeframe.M5][:-1], extreme),
            }
        }
    )
    first = ShadowDecisionEngine().evaluate(snapshot, risk=_engine_risk(snapshot))
    second = ShadowDecisionEngine().evaluate(changed, risk=_engine_risk(changed))
    assert first.decision == second.decision
    assert first.m5_candle_timestamp == second.m5_candle_timestamp


def test_closed_only_upstream_uses_newest_closed_candle(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    closed_only = snapshot.model_copy(
        update={
            "candles": {
                timeframe: values[:-1] for timeframe, values in snapshot.candles.items()
            }
        }
    )
    decision = ShadowDecisionEngine().evaluate(
        closed_only,
        risk=_engine_risk(closed_only),
        candles_are_closed=True,
    )
    assert decision.m5_candle_timestamp == closed_only.candles[Timeframe.M5][-1].timestamp


def test_feature_validation_rejects_short_window(model_parts) -> None:
    with pytest.raises(FeatureValidationError):
        extract_features(_trend_snapshot(model_parts).candles[Timeframe.M5][:2], timeframe="M5")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"data_freshness": "STALE"}, "DATA_STALE"),
        ({"mt5_state": "UNKNOWN"}, "MT5_NOT_CONNECTED"),
        ({"runtime_state": "DEGRADED"}, "RUNTIME_NOT_CONNECTED"),
        ({"risk": None}, "RISK_STATE_UNKNOWN"),
    ],
)
def test_unsafe_state_is_no_trade(model_parts, kwargs, reason) -> None:
    snapshot = _trend_snapshot(model_parts)
    options = {"risk": _engine_risk(snapshot), **kwargs}
    decision = ShadowDecisionEngine().evaluate(snapshot, **options)
    assert decision.decision == ShadowAction.NO_TRADE
    assert reason in decision.reason_codes


def test_unknown_and_unbounded_risk_are_blocked(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    risk = _engine_risk(snapshot)
    unknown = risk.model_copy(update={"open_risk_percent": None, "remaining_risk_percent": None})
    unbounded = risk.model_copy(update={"unbounded_positions_count": 1})
    engine = ShadowDecisionEngine()
    assert "UNKNOWN_RISK" in engine.evaluate(snapshot, risk=unknown).reason_codes
    assert "UNBOUNDED_EXISTING_POSITION" in engine.evaluate(snapshot, risk=unbounded).reason_codes


def test_minimum_lot_and_aggregate_budget_are_blocked(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    engine = ShadowDecisionEngine()
    risk = _engine_risk(snapshot)
    high_min = snapshot.symbol.model_copy(update={"volume_min": 100.0})
    too_large = snapshot.model_copy(update={"symbol": high_min})
    assert (
        "MIN_LOT_EXCEEDS_RISK_BUDGET"
        in engine.evaluate(too_large, risk=_engine_risk(too_large)).reason_codes
    )
    exhausted = risk.model_copy(update={"open_risk_percent": 5.99, "remaining_risk_percent": 0.01})
    assert engine.evaluate(snapshot, risk=exhausted).decision == ShadowAction.NO_TRADE


def test_volume_is_normalized_downward_and_rr_is_explicit(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    decision = ShadowDecisionEngine().evaluate(snapshot, risk=_engine_risk(snapshot))
    assert decision.decision in {ShadowAction.BUY, ShadowAction.SELL}
    assert decision.hypothetical_volume is not None
    assert decision.hypothetical_volume / snapshot.symbol.volume_step == pytest.approx(
        round(decision.hypothetical_volume / snapshot.symbol.volume_step)
    )
    assert decision.risk_reward_ratio is not None and decision.risk_reward_ratio >= 2


def test_rr_below_configured_minimum_is_blocked(model_parts) -> None:
    snapshot = _trend_snapshot(model_parts)
    decision = ShadowDecisionEngine(min_rr=3.0, target_rr=2.0).evaluate(
        snapshot, risk=_engine_risk(snapshot)
    )
    assert decision.decision == ShadowAction.NO_TRADE
    assert "RR_TOO_LOW" in decision.reason_codes


def test_persistence_is_idempotent_and_execution_false(
    shadow_database: Database, model_parts
) -> None:
    snapshot = _trend_snapshot(model_parts)
    decision = ShadowDecisionEngine().evaluate(
        snapshot, market_snapshot_id=None, risk=_engine_risk(snapshot)
    )
    repository = ShadowDecisionRepository(shadow_database)
    first = repository.persist(decision)
    second = repository.persist(decision.model_copy(update={"decision_id": uuid4()}))
    assert first.id == second.id
    with shadow_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ShadowDecisionRecord)) == 1
        assert session.scalar(select(ShadowDecisionRecord.execution_allowed)) is False


def test_api_and_telegram_render_shadow_decision(
    shadow_database: Database, model_parts, tmp_path: Path
) -> None:
    snapshot = _trend_snapshot(model_parts)
    decision = ShadowDecisionEngine().evaluate(snapshot, risk=_engine_risk(snapshot))
    ShadowDecisionRepository(shadow_database).persist(decision)
    with TestClient(create_app(settings=Settings(), database=shadow_database)) as client:
        payload = client.get("/api/shadow/decision").json()
        summary = client.get("/api/shadow/summary").json()
    assert payload["decision"] == decision.decision.value
    assert payload["execution_allowed"] is False
    assert summary["total"] == 1
    service = TelegramControlService(Settings(), tmp_path, database=shadow_database)
    assert "SHADOW ONLY" in service._decision()
    assert "Execution: DISABLED" in service._shadow()


def test_replay_is_chronological_and_deduplicated(model_parts) -> None:
    first = _trend_snapshot(model_parts)
    shifted_candles = {
        timeframe: tuple(
            candle.model_copy(update={"timestamp": candle.timestamp + timedelta(minutes=5)})
            for candle in values
        )
        for timeframe, values in first.candles.items()
    }
    second = first.model_copy(
        update={
            "generated_at": first.generated_at + timedelta(minutes=5),
            "candles": shifted_candles,
        }
    )
    risk = _engine_risk(first)
    report = replay_snapshots([(second, None, risk), (first, None, risk), (first, None, risk)])
    assert report.candles_processed == 3
    assert report.decisions_generated == 2
    assert report.duplicates == 1
    assert report.errors == 0


@pytest.mark.asyncio
async def test_shadow_worker_persists_consecutive_no_trade_candles(shadow_database, model_parts):
    first = _trend_snapshot(model_parts)
    conflicting = first.model_copy(
        update={
            "candles": {
                **first.candles,
                Timeframe.H1: _trend_snapshot(model_parts, "down").candles[Timeframe.H1],
            }
        }
    )
    worker = ShadowDecisionWorker(
        Settings(),
        shadow_database,
        EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    for index in range(5):
        shifted = conflicting.model_copy(
            update={
                "candles": {
                    timeframe: tuple(
                        candle.model_copy(
                            update={"timestamp": candle.timestamp + timedelta(minutes=index * 5)}
                        )
                        for candle in values
                    )
                    for timeframe, values in conflicting.candles.items()
                }
            }
        )
        await worker._process(ShadowInput(shifted, None, _engine_risk(shifted), True))
    rows = ShadowDecisionRepository(shadow_database).list(limit=10)
    assert len(rows) == 5
    assert all(row.decision == "NO_TRADE" for row in rows)
    assert all(row.execution_allowed is False for row in rows)


@pytest.mark.asyncio
async def test_shadow_worker_strategy_exception_recovers_on_next_candle(
    shadow_database, model_parts
):
    first = _trend_snapshot(model_parts)
    second = first.model_copy(
        update={
            "candles": {
                timeframe: tuple(
                    candle.model_copy(update={"timestamp": candle.timestamp + timedelta(minutes=5)})
                    for candle in values
                )
                for timeframe, values in first.candles.items()
            }
        }
    )
    worker = ShadowDecisionWorker(
        Settings(),
        shadow_database,
        EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    original = worker.engine.evaluate
    calls = 0

    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic strategy failure")
        return original(*args, **kwargs)

    worker.engine.evaluate = flaky
    await worker._process(ShadowInput(first, None, _engine_risk(first), True))
    await worker._process(ShadowInput(second, None, _engine_risk(second), True))
    assert ShadowDecisionRepository(shadow_database).latest() is not None
    with shadow_database.session() as session:
        health = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "worker:shadow")
            .order_by(SystemHealthRecord.timestamp.desc())
            .limit(1)
        )
    assert health is not None and health.status == "CONNECTED"


def test_shadow_queue_full_is_observable_not_silent(shadow_database, model_parts):
    worker = ShadowDecisionWorker(
        Settings(),
        shadow_database,
        EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    first = _trend_snapshot(model_parts)
    for index in range(9):
        snapshot = first.model_copy(
            update={
                "candles": {
                    timeframe: tuple(
                        candle.model_copy(
                            update={"timestamp": candle.timestamp + timedelta(minutes=index * 5)}
                        )
                        for candle in values
                    )
                    for timeframe, values in first.candles.items()
                }
            }
        )
        worker.submit(ShadowInput(snapshot, None, _engine_risk(snapshot), True))
    assert worker.queue_depth == 8
    assert worker.deferred_count == 1
    assert worker.total_backlog == 9
    with shadow_database.session() as session:
        health = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "worker:shadow")
            .order_by(SystemHealthRecord.timestamp.desc())
            .limit(1)
        )
    assert health is not None and health.status == "DEGRADED"
    assert health.metadata_json["error_category"] == "QueueFull"


@pytest.mark.asyncio
async def test_shadow_latest_reader_uses_event_time_after_out_of_order_catchup(
    shadow_database, model_parts
):
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    newer = _trend_snapshot(model_parts)
    older = newer.model_copy(
        update={
            "candles": {
                timeframe: tuple(
                    candle.model_copy(update={"timestamp": candle.timestamp - timedelta(minutes=5)})
                    for candle in values
                )
                for timeframe, values in newer.candles.items()
            }
        }
    )
    await worker._process(ShadowInput(newer, None, _engine_risk(newer), True))
    await worker._process(ShadowInput(older, None, _engine_risk(older), True))
    latest = ShadowDecisionRepository(shadow_database).latest()
    assert latest is not None
    assert latest.m5_candle_timestamp == ShadowDecisionEngine._m5_timestamp(
        newer, candles_are_closed=True
    )
    ordered = [row.m5_candle_timestamp for row in ShadowDecisionRepository(shadow_database).list()]
    assert ordered == [
        latest.m5_candle_timestamp,
        ShadowDecisionEngine._m5_timestamp(older, candles_are_closed=True),
    ]


def test_shadow_health_exposes_bounded_backlog_fields(shadow_database):
    SystemHealthRepository(shadow_database).record(
        "worker:shadow",
        "CATCHING_UP",
        metadata={
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "latest_available_m5": "2026-09-22T06:35:00+00:00",
            "latest_received_m5": "2026-09-22T06:35:00+00:00",
            "latest_processed_m5": "2026-09-22T06:25:00+00:00",
            "latest_decision_m5": "2026-09-22T06:25:00+00:00",
            "queue_depth": 8,
            "queue_capacity": 8,
            "deferred_count": 1,
            "catchup_pending_count": 3,
            "total_backlog": 12,
            "execution_allowed": False,
        },
    )
    with TestClient(create_app(settings=Settings(), database=shadow_database)) as client:
        payload = client.get("/api/shadow/health").json()
    assert payload["state"] == "CATCHING_UP"
    assert payload["queue_depth"] == 8
    assert payload["queue_capacity"] == 8
    assert payload["total_backlog"] == 12


def test_shadow_watermarks_never_move_backwards(shadow_database):
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    newest = datetime(2026, 9, 22, 6, 35, tzinfo=UTC)
    older = newest - timedelta(minutes=5)
    worker._set_max("_latest_received_m5", newest)
    worker._set_max("_latest_received_m5", older)
    worker._set_max("_latest_processed_m5", newest)
    worker._set_max("_latest_processed_m5", older)
    assert worker._latest_received_m5 == newest
    assert worker._latest_processed_m5 == newest


def test_shadow_catching_up_state_requires_recent_progress(shadow_database):
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    worker._task = object()
    worker._deferred_keys.add(("XAUUSDm", datetime.now(UTC), worker.engine.strategy_version))
    worker._last_success_at = datetime.now(UTC)
    assert worker.state == "CATCHING_UP"
    worker._last_success_at = datetime.now(UTC) - timedelta(seconds=worker.stall_ttl_seconds + 1)
    assert worker.state == "DEGRADED"


def test_shadow_error_recovers_after_successful_cycle(shadow_database, model_parts):
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    worker._task = object()
    worker._failure_count = 3
    worker._last_failed_at = datetime.now(UTC)
    assert worker.state == "ERROR"
    snapshot = _trend_snapshot(model_parts)
    asyncio.run(worker._process(ShadowInput(snapshot, None, _engine_risk(snapshot), True)))
    assert worker.state == "CONNECTED"


def test_shadow_health_is_consistent_across_api_and_telegram(shadow_database, tmp_path):
    SystemHealthRepository(shadow_database).record(
        "worker:shadow",
        "CONNECTED",
        message="heartbeat",
        metadata={
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "last_received_candle_at": "2026-09-22T06:00:00+00:00",
            "last_processed_candle_at": "2026-09-22T06:00:00+00:00",
            "last_decision_at": "2026-09-22T06:00:00+00:00",
            "queue_depth": 0,
            "execution_allowed": False,
        },
    )
    settings = Settings()
    with TestClient(create_app(settings=settings, database=shadow_database)) as client:
        health = client.get("/api/system/health").json()
        shadow_health = client.get("/api/shadow/health").json()
    telegram = TelegramControlService(settings, tmp_path, database=shadow_database)
    websocket_health = RealtimeHub(shadow_database)._fetch_new_health()
    assert health["services"]["shadow_worker"] == "CONNECTED"
    assert shadow_health["state"] == "CONNECTED"
    assert shadow_health["last_processed_candle_at"] == "2026-09-22T06:00:00+00:00"
    assert (
        next(item for item in websocket_health if item["component"] == "worker:shadow")["status"]
        == "CONNECTED"
    )
    assert "Worker:Shadow   CONNECTED" in telegram._health()
    assert "State: CONNECTED" in telegram._shadowhealth()


@pytest.mark.asyncio
async def test_shadow_heartbeat_does_not_stall_without_new_market_data(shadow_database):
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    worker.start()
    await asyncio.sleep(0.05)
    assert worker.state == "CONNECTED"
    await worker.stop()


def test_shadow_new_data_stall_is_degraded(shadow_database):
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    worker._task = object()
    worker._last_received_candle = datetime.now(UTC) - timedelta(
        seconds=worker.stall_ttl_seconds + 1
    )
    assert worker.state == "DEGRADED"


@pytest.mark.asyncio
async def test_shadow_restart_catchup_is_chronological_and_idempotent(
    shadow_database, model_parts, monkeypatch
):
    first = _trend_snapshot(model_parts)
    second = first.model_copy(
        update={
            "candles": {
                timeframe: tuple(
                    candle.model_copy(update={"timestamp": candle.timestamp + timedelta(minutes=5)})
                    for candle in values
                )
                for timeframe, values in first.candles.items()
            }
        }
    )
    monkeypatch.setattr(
        "services.shadow_service.load_persisted_snapshots",
        lambda _database, limit=None: [
            (second, None, _engine_risk(second)),
            (first, None, _engine_risk(first)),
        ],
    )
    worker = ShadowDecisionWorker(
        Settings(), shadow_database, EventBus(logging.getLogger("shadow-test")),
        logger=logging.getLogger("shadow-test"),
    )
    await worker._catch_up()
    queued = [worker._queue.get_nowait(), worker._queue.get_nowait()]
    timestamps = [worker._key(item)[1] for item in queued]
    assert timestamps == sorted(timestamps)
    await worker._catch_up()
    assert worker.queue_depth == 0
