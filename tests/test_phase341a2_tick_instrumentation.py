from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

import pytest

import services.live as live_module
from config.settings import Settings
from domain.events import DomainEvent, EventType
from events.bus import EventBus
from mt5.gateway import MT5Gateway
from persistence.database import Database
from persistence.orm import SystemEventRecord
from persistence.repositories import EventRepository
from services.live import LiveDataEngine
from services.tick_diagnostics import TickPollingDiagnostics


def _diagnostics() -> TickPollingDiagnostics:
    return TickPollingDiagnostics(stale_threshold_seconds=10, slow_threshold_seconds=2)


def _sample(
    collector: TickPollingDiagnostics, at: datetime, monotonic: float, *, source=None
) -> None:
    collector.begin_attempt(at, monotonic)
    collector.mark_stage("MT5_GATEWAY_OPERATION", at, monotonic + 0.01)
    collector.mark_stage("MT5_TICK_READ", at, monotonic + 0.01)
    collector.record_tick_read(
        source_timestamp=source or at,
        completed_at=at + timedelta(milliseconds=20),
        completed_monotonic=monotonic + 0.02,
    )
    collector.finish_attempt(
        completed_at=at + timedelta(milliseconds=40),
        completed_monotonic=monotonic + 0.04,
        observed_at=at + timedelta(milliseconds=30),
        gateway_wait_ms=10,
        gateway_operation_ms=20,
        positions_read_ms=5,
    )


def test_poll_delay_and_source_age_are_diagnostic_only() -> None:
    collector = _diagnostics()
    now = datetime(2026, 9, 24, tzinfo=UTC)
    _sample(collector, now, 10.0, source=now - timedelta(seconds=12))
    info = collector.snapshot(now=now + timedelta(milliseconds=100), monotonic=10.1)
    assert info["threshold_seconds"] == 10
    assert info["diagnostic_classification"] == "SOURCE_TICK_STALE"
    assert info["source_tick_age_ms"] == 12_030
    collector.begin_attempt(now + timedelta(seconds=4), 14.0)
    collector.mark_stage("MT5_GATEWAY_OPERATION", now + timedelta(seconds=4), 14.01)
    late = collector.snapshot(now=now + timedelta(seconds=4), monotonic=14.1)
    assert late["last_loop_gap_ms"] == 4_000
    assert late["diagnostic_classification"] == "POLL_DELAY"


def test_gateway_wait_and_tick_read_delay_are_distinguished() -> None:
    at = datetime(2026, 9, 24, tzinfo=UTC)
    waiting = _diagnostics()
    waiting.begin_attempt(at, 0.0)
    result = waiting.snapshot(now=at + timedelta(seconds=2.5), monotonic=2.5)
    assert result["diagnostic_classification"] == "MT5_ACCESS_WAIT"

    reading = _diagnostics()
    reading.begin_attempt(at, 0.0)
    reading.mark_stage("MT5_GATEWAY_OPERATION", at, 0.01)
    reading.mark_stage("MT5_TICK_READ", at, 0.02)
    result = reading.snapshot(now=at + timedelta(seconds=2.2), monotonic=2.22)
    assert result["diagnostic_classification"] == "MT5_READ_DELAY"
    assert result["active_stage"] == "MT5_TICK_READ"


def test_consecutive_tick_read_failures_recover_and_storage_is_bounded() -> None:
    collector = _diagnostics()
    at = datetime(2026, 9, 24, tzinfo=UTC)
    for index in range(3):
        started = at + timedelta(seconds=index)
        collector.begin_attempt(started, float(index))
        collector.mark_stage("MT5_TICK_READ", started, float(index) + 0.01)
        collector.record_tick_read_failure(
            started + timedelta(milliseconds=50), float(index) + 0.05, "TickReadError"
        )
        collector.finish_attempt(
            completed_at=started + timedelta(milliseconds=50),
            completed_monotonic=float(index) + 0.05,
            observed_at=None,
            gateway_wait_ms=5,
            gateway_operation_ms=40,
            positions_read_ms=None,
            error_category="TickReadError",
            tick_read_failed=True,
        )
    failed = collector.snapshot(now=at + timedelta(seconds=3), monotonic=3.0)
    assert failed["consecutive_read_failures"] == 3
    assert failed["diagnostic_classification"] == "READ_FAILURE"
    _sample(collector, at + timedelta(seconds=3), 3.0)
    recovered = collector.snapshot(now=at + timedelta(seconds=3, milliseconds=100), monotonic=3.1)
    assert recovered["consecutive_read_failures"] == 0
    assert recovered["diagnostic_classification"] == "NORMAL"
    assert len(collector._last) < 30
    assert len(collector._persistence) == 0


@pytest.mark.asyncio
async def test_gateway_timing_keeps_serialized_call_boundary() -> None:
    gateway = MT5Gateway(Settings(), module=object(), logger=logging.getLogger("timed-gateway"))
    gateway._connected = True
    gateway._connection._connected = True
    gateway._connection._module = object()
    await gateway._lock.acquire()
    task = asyncio.create_task(gateway.call_timed(lambda _api: "ok"))
    await asyncio.sleep(0.02)
    gateway._lock.release()
    result, timing = await task
    assert result == "ok"
    assert timing.waiting_for_access_ms >= 10
    assert timing.operation_ms >= 0


@pytest.mark.asyncio
async def test_gateway_diagnostic_callback_failure_is_redacted_and_nonblocking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "token-that-must-never-be-logged"
    gateway = MT5Gateway(
        Settings(), module=object(), logger=logging.getLogger("gateway-diagnostic-redaction")
    )
    gateway._connected = True
    gateway._connection._connected = True
    gateway._connection._module = object()
    caplog.set_level(logging.DEBUG, logger="gateway-diagnostic-redaction")
    operation_calls = []

    def broken_callback(_stage: str, _monotonic: float) -> None:
        raise RuntimeError(secret)

    result, timing = await gateway.call_timed(
        lambda _api: operation_calls.append("called") or "operation-result",
        on_stage=broken_callback,
    )

    assert result == "operation-result"
    assert operation_calls == ["called"]
    assert timing.operation_ms >= 0
    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)

    operation_failure = RuntimeError(secret)

    def fail_operation(_api):
        raise operation_failure

    with pytest.raises(RuntimeError) as raised:
        await gateway.call_timed(fail_operation, on_stage=broken_callback)
    assert raised.value is operation_failure
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_live_diagnostic_collection_failure_is_redacted_and_nonblocking(
    tmp_path: Path,
    model_parts,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _account, symbol, tick, _candles, _position = model_parts
    tick = tick.model_copy(update={"timestamp": datetime.now(UTC)})
    secret = "diagnostic-secret-value"
    database = Database(f"sqlite:///{(tmp_path / 'diagnostic-failure.db').as_posix()}")
    database.create_schema()
    operation_calls = []

    class BrokenDiagnostics:
        def __getattr__(self, _name):
            def fail(*_args, **_kwargs):
                raise RuntimeError(secret)

            return fail

    class FakeGateway:
        async def call_timed(self, operation, *, on_stage=None):
            if on_stage:
                on_stage("WAITING_FOR_MT5_ACCESS", perf_counter())
                on_stage("MT5_GATEWAY_OPERATION", perf_counter())
            result = operation(None)
            return result, type("Timing", (), {"waiting_for_access_ms": 1.0, "operation_ms": 2.0})()

    monkeypatch.setattr(
        "services.live.read_tick", lambda _api, _symbol: operation_calls.append("tick") or tick
    )
    monkeypatch.setattr("services.live.read_open_positions", lambda _api, _symbol: ())
    logger = logging.getLogger("live-diagnostic-redaction")
    caplog.set_level(logging.DEBUG, logger="live-diagnostic-redaction")
    engine = LiveDataEngine(
        Settings(), database, EventBus(logger), logger=logger, gateway=FakeGateway()
    )
    engine._tick_diagnostics = BrokenDiagnostics()
    engine.state.symbol = symbol.name
    try:
        asyncio.run(engine._fast_loop())
        assert operation_calls == ["tick"]
        assert engine.state.tick == tick
        assert secret not in caplog.text
        assert "Traceback" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        database.dispose()


def test_fast_poll_emits_no_event_and_records_read_diagnostics(
    tmp_path: Path, model_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account, symbol, tick, _candles, _position = model_parts
    tick = tick.model_copy(update={"timestamp": datetime.now(UTC)})
    database = Database(f"sqlite:///{(tmp_path / 'tick-poll.db').as_posix()}")
    database.create_schema()

    class FakeGateway:
        async def call_timed(self, operation, *, on_stage=None):
            before = perf_counter()
            if on_stage:
                on_stage("WAITING_FOR_MT5_ACCESS", before)
                on_stage("MT5_GATEWAY_OPERATION", before + 0.001)
            result = operation(None)
            if on_stage:
                on_stage("MT5_GATEWAY_OPERATION_COMPLETED", perf_counter())
            return result, type("Timing", (), {"waiting_for_access_ms": 1.0, "operation_ms": 2.0})()

    monkeypatch.setattr("services.live.read_tick", lambda _api, _symbol: tick)
    monkeypatch.setattr("services.live.read_open_positions", lambda _api, _symbol: ())
    engine = LiveDataEngine(
        Settings(),
        database,
        EventBus(logging.getLogger("tick-fast-loop")),
        logger=logging.getLogger("tick-fast-loop"),
        gateway=FakeGateway(),
    )
    engine.state.symbol = symbol.name
    try:
        asyncio.run(engine._fast_loop())
        assert engine.state.tick == tick
        info = engine._tick_diagnostics.snapshot(now=datetime.now(UTC), monotonic=perf_counter())
        assert info["diagnostic_classification"] == "NORMAL"
        assert info["mt5_gateway_operation_ms"] == 2.0
        assert info["waiting_for_mt5_access_ms"] == 1.0
        with database.session() as session:
            assert session.query(SystemEventRecord).count() == 0
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_stale_transition_carries_sparse_diagnostics_without_changing_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    class FakeHealth:
        def record(self, *_args, **_kwargs):
            return "health-id"

    database = Database(f"sqlite:///{(tmp_path / 'tick-stale.db').as_posix()}")
    database.create_schema()
    captured: list[DomainEvent] = []
    bus = EventBus(logging.getLogger("tick-watchdog"))

    async def capture(event: DomainEvent) -> None:
        captured.append(event)

    bus.subscribe("capture", capture)
    bus.subscribe("database", EventRepository(database).handle, critical=True)
    engine = LiveDataEngine(Settings(), database, bus, logger=logging.getLogger("tick-watchdog"))
    engine.health = FakeHealth()
    engine.state.last_tick_observed = now - timedelta(seconds=11)
    engine._tick_diagnostics.seed_observation(
        now - timedelta(seconds=11), now - timedelta(seconds=35)
    )
    monkeypatch.setattr(live_module, "datetime", FrozenDateTime)

    async def stop_after_iteration(_seconds):
        engine._stop.set()

    monkeypatch.setattr(live_module.asyncio, "sleep", stop_after_iteration)
    try:
        await engine._watchdog_loop()
        stale = [event for event in captured if event.event_type is EventType.DATA_STALE]
        assert len(stale) == 1
        assert stale[0].payload.diagnostics["threshold_seconds"] == 10
        assert stale[0].payload.diagnostics["diagnostic_classification"] == "POLL_DELAY"
        with database.session() as session:
            row = session.query(SystemEventRecord).one()
            assert row.payload["diagnostics"]["threshold_seconds"] == 10
        engine._stop = asyncio.Event()
        await engine._watchdog_loop()
        assert len([event for event in captured if event.event_type is EventType.DATA_STALE]) == 1
        engine._stop = asyncio.Event()
        engine.state.last_tick_observed = now
        await engine._watchdog_loop()
        assert "tick" not in engine._stale_components
        engine._stop = asyncio.Event()
        engine.state.last_tick_observed = now - timedelta(seconds=11)
        await engine._watchdog_loop()
        assert len([event for event in captured if event.event_type is EventType.DATA_STALE]) == 2
    finally:
        database.dispose()
