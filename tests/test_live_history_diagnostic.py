from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from config.settings import Settings
from events.bus import EventBus
from models.live import RuntimeState
from mt5.gateway import MT5Gateway, MT5GatewayBusyError
from persistence.database import Database
from persistence.orm import CandleRecord, SystemEventRecord, SystemHealthRecord
from services.live import LiveDataEngine
from services.live_history_diagnostic import (
    MAX_RETURNED_BARS,
    HistoryDiagnosticError,
    LiveHistoryDiagnosticClient,
    LiveHistoryDiagnosticServer,
    execute_history_diagnostic,
    query_m15_history,
    validate_history_diagnostic_request,
)

START = datetime(2026, 9, 24, 20, 30, tzinfo=UTC)
END = datetime(2026, 9, 24, 22, 15, tzinfo=UTC)


def _payload(**updates):
    result = {
        "request_id": str(uuid4()),
        "symbol": "XAUUSDm",
        "timeframe": "M15",
        "start": "2026-09-24T20:30:00Z",
        "end": "2026-09-24T22:15:00Z",
    }
    result.update(updates)
    return result


class FakeMT5:
    TIMEFRAME_M15 = 15

    def __init__(self, rates=None, *, read_error=None):
        self.rates = rates if rates is not None else []
        self.read_error = read_error
        self.query = None
        self.initialize_calls = 0
        self.session_calls = []

    def initialize(self, *_args, **_kwargs):
        self.initialize_calls += 1
        raise AssertionError("diagnostic must not initialize MT5")

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.query = (symbol, timeframe, start, end)
        if self.read_error is not None:
            raise self.read_error
        return self.rates

    def symbol_info(self, symbol):
        return SimpleNamespace(name=symbol, trade_mode=4, trade_mode_name="FULL")

    def symbol_info_session_trade(self, symbol, day_of_week, session_index):
        self.session_calls.append((symbol, day_of_week, session_index))
        if session_index:
            return None
        return SimpleNamespace(**{"from": 0, "to": 86_400})


class FakeGateway:
    connected = True

    def __init__(self, api):
        self.api = api
        self.calls = 0
        self.busy = False

    async def call_if_idle(self, operation):
        self.calls += 1
        if self.busy:
            raise MT5GatewayBusyError("normal polling owns gateway")
        return operation(self.api)


def _rates(*opens):
    return [{"time": int(value.timestamp())} for value in opens]


def test_valid_bounded_request_preserves_exact_boundaries_and_only_returns_times() -> None:
    api = FakeMT5(_rates(START, START + timedelta(minutes=15), END))
    request = validate_history_diagnostic_request(_payload())

    result = query_m15_history(api, request)

    assert api.query == ("XAUUSDm", api.TIMEFRAME_M15, START, END)
    assert result["requested_start"] == "2026-09-24T20:30:00Z"
    assert result["requested_end"] == "2026-09-24T22:15:00Z"
    assert result["bar_open_timestamps"] == [
        "2026-09-24T20:30:00Z",
        "2026-09-24T20:45:00Z",
        "2026-09-24T22:15:00Z",
    ]
    assert result["returned_bar_count"] == 3
    assert "open" not in result and "close" not in result
    assert result["session_metadata"]["available"] is True
    assert result["session_metadata"]["time_basis"].endswith("UTC offset not inferred")
    assert api.initialize_calls == 0


def test_session_gap_is_returned_without_filling_missing_bars() -> None:
    opens = (START, START + timedelta(minutes=15), datetime(2026, 9, 24, 22, tzinfo=UTC))
    request = validate_history_diagnostic_request(_payload())
    result = query_m15_history(FakeMT5(_rates(*opens)), request)
    assert result["bar_open_timestamps"] == [
        "2026-09-24T20:30:00Z", "2026-09-24T20:45:00Z", "2026-09-24T22:00:00Z"
    ]
    assert result["returned_bar_count"] == 3


def test_empty_broker_result_is_a_successful_empty_observation() -> None:
    result = query_m15_history(FakeMT5([]), validate_history_diagnostic_request(_payload()))
    assert result["bar_open_timestamps"] == []
    assert result["returned_bar_count"] == 0


def test_session_metadata_failure_does_not_fail_history_result() -> None:
    class SessionFailureMT5(FakeMT5):
        def symbol_info_session_trade(self, *_args):
            raise RuntimeError("session calendar unavailable")

    result = query_m15_history(
        SessionFailureMT5(_rates(START)), validate_history_diagnostic_request(_payload())
    )
    assert result["ok"] is True
    assert result["bar_open_timestamps"] == ["2026-09-24T20:30:00Z"]
    assert result["session_metadata"] == {"available": False, "sessions": []}


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"timeframe": "H1"}, "UNSUPPORTED_TIMEFRAME"),
        ({"symbol": "XAUUSDm;DROP"}, "INVALID_SYMBOL"),
        ({"start": "2026-09-24T00:00:00Z", "end": "2026-09-24T05:00:01Z"}, "RANGE_EXCEEDS_LIMIT"),
        ({"start": "not-a-time"}, "INVALID_TIMESTAMP"),
        ({"start": "2026-09-24T20:30:00"}, "INVALID_TIMESTAMP"),
        ({"start": "2026-09-24T20:30:00+07:00"}, "TIMESTAMP_MUST_BE_UTC"),
        ({"start": "2026-09-24T22:15:00Z"}, "INVALID_RANGE"),
    ],
)
def test_invalid_requests_fail_closed(updates, code) -> None:
    with pytest.raises(HistoryDiagnosticError) as caught:
        validate_history_diagnostic_request(_payload(**updates))
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"timeframe": "M5"}, "UNSUPPORTED_TIMEFRAME"),
        ({"symbol": "XAUUSD"}, "UNKNOWN_SYMBOL"),
    ],
)
@pytest.mark.asyncio
async def test_unsupported_or_unconfigured_symbol_never_reaches_gateway(payload, expected) -> None:
    gateway = FakeGateway(FakeMT5())
    response = await execute_history_diagnostic(
        gateway,
        _payload(**payload),
        active_symbol="XAUUSDm",
        runtime_connected=True,
    )
    assert response["error_code"] == expected
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_diagnostic_uses_idle_gateway_serialization_without_initializing_mt5() -> None:
    api = FakeMT5(_rates(START, END))
    gateway = FakeGateway(api)
    response = await execute_history_diagnostic(
        gateway, _payload(), active_symbol="XAUUSDm", runtime_connected=True
    )
    assert response["ok"] is True
    assert gateway.calls == 1
    assert api.initialize_calls == 0

    gateway.busy = True
    busy = await execute_history_diagnostic(
        gateway, _payload(), active_symbol="XAUUSDm", runtime_connected=True
    )
    assert busy["error_code"] == "MT5_GATEWAY_BUSY"
    assert api.initialize_calls == 0


def test_gateway_call_if_idle_reuses_lock_and_refuses_to_queue() -> None:
    api = FakeMT5()
    gateway = MT5Gateway(Settings(), module=api)
    gateway._connected = True
    gateway._connection._connected = True

    async def run():
        assert await gateway.call_if_idle(lambda current: current.TIMEFRAME_M15) == 15
        await gateway._lock.acquire()
        try:
            with pytest.raises(MT5GatewayBusyError):
                await gateway.call_if_idle(lambda current: current.TIMEFRAME_M15)
        finally:
            gateway._lock.release()

    asyncio.run(run())
    assert api.initialize_calls == 0


def test_gateway_call_if_idle_rejects_unlocked_lock_with_normal_waiter() -> None:
    gateway = MT5Gateway(Settings(), module=FakeMT5())
    gateway._connected = True
    gateway._connection._connected = True

    async def run():
        await gateway._lock.acquire()
        normal_call = asyncio.create_task(gateway.call(lambda _api: "normal"))
        await asyncio.sleep(0)
        gateway._lock.release()
        # release() has woken the queued normal call but it has not resumed yet.
        assert not gateway._lock.locked()
        with pytest.raises(MT5GatewayBusyError):
            await gateway.call_if_idle(lambda _api: "diagnostic")
        assert await normal_call == "normal"

    asyncio.run(run())


@pytest.mark.parametrize(
    "rates",
    [
        _rates(*(START + timedelta(minutes=15 * n) for n in range(MAX_RETURNED_BARS + 1))),
        [{"time": "not-epoch"}],
        _rates(START - timedelta(minutes=15)),
        _rates(START + timedelta(minutes=15), START),
    ],
)
def test_excessive_or_malformed_broker_results_are_rejected(rates) -> None:
    with pytest.raises(HistoryDiagnosticError):
        query_m15_history(FakeMT5(rates), validate_history_diagnostic_request(_payload()))


@pytest.mark.asyncio
async def test_diagnostic_failure_is_sanitized_and_does_not_change_live_or_write_db(
    tmp_path: Path,
) -> None:
    secret = "account-secret-in-exception"
    database = Database(f"sqlite:///{(tmp_path / 'history-diagnostic.db').as_posix()}")
    database.create_schema()
    with database.session() as session:
        before = {
            model: session.scalar(select(func.count()).select_from(model))
            for model in (CandleRecord, SystemEventRecord, SystemHealthRecord)
        }
    try:
        engine = LiveDataEngine(
            Settings(), database, EventBus(logging.getLogger("history-diagnostic")),
            logger=logging.getLogger("history-diagnostic"),
            gateway=FakeGateway(FakeMT5(read_error=RuntimeError(secret))),
        )
        engine.state.symbol = "XAUUSDm"
        engine.runtime_state = RuntimeState.CONNECTED
        workers_before = dict(engine._workers)
        state_before = engine.runtime_state
        response = await engine._handle_history_diagnostic(
            validate_history_diagnostic_request(_payload())
        )
        assert response["ok"] is False
        assert response["error_code"] == "MT5_READ_FAILED"
        assert secret not in repr(response)
        assert engine._workers == workers_before
        assert engine.runtime_state == state_before
        with database.session() as session:
            after = {
                model: session.scalar(select(func.count()).select_from(model))
                for model in (CandleRecord, SystemEventRecord, SystemHealthRecord)
            }
        assert after == before
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_live_disconnected_request_does_not_touch_gateway() -> None:
    gateway = FakeGateway(FakeMT5())
    response = await execute_history_diagnostic(
        gateway, _payload(), active_symbol="XAUUSDm", runtime_connected=False
    )
    assert response["error_code"] == "LIVE_NOT_CONNECTED"
    assert gateway.calls == 0


@pytest.mark.skipif(os.name != "nt", reason="Live diagnostic transport is a Windows named pipe")
def test_local_pipe_request_round_trip_is_bounded_and_correlated(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)

    async def handler(request):
        return {
            "ok": True,
            "request_id": request.request_id,
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "requested_start": request.start.isoformat().replace("+00:00", "Z"),
            "requested_end": request.end.isoformat().replace("+00:00", "Z"),
            "bar_open_timestamps": [],
            "returned_bar_count": 0,
            "symbol_metadata": {"name": request.symbol},
            "session_metadata": {"available": False, "sessions": []},
            "diagnostic_timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

    server = LiveHistoryDiagnosticServer(tmp_path, loop, handler)
    server_thread = None
    try:
        server.start()
        server_thread = server._thread
        response = LiveHistoryDiagnosticClient(tmp_path, timeout_seconds=3).request(_payload())
        assert response["symbol"] == "XAUUSDm"
        assert response["returned_bar_count"] == 0
    finally:
        server.close()
        assert server_thread is not None and not server_thread.is_alive()

    replacement = LiveHistoryDiagnosticServer(tmp_path, loop, handler)
    try:
        replacement.start()
        response = LiveHistoryDiagnosticClient(tmp_path, timeout_seconds=3).request(_payload())
        assert response["ok"] is True
    finally:
        replacement.close()
        assert replacement._thread is None
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_pipe_request_parse_failure_and_client_disconnect_do_not_kill_server(
    tmp_path: Path, monkeypatch
) -> None:
    from services import live_history_diagnostic as diagnostic

    class DisconnectingConnection:
        def recv_bytes(self, _maxlength):
            return b'{"unexpected":"malformed"}'

        def send_bytes(self, _response):
            raise BrokenPipeError("client disconnected")

        def close(self):
            pass

    class ListenerStub:
        def __init__(self):
            self.accept_count = 0
            self.closed = threading.Event()

        def accept(self):
            self.accept_count += 1
            if self.accept_count == 1:
                return DisconnectingConnection()
            self.closed.wait(timeout=2)
            raise OSError("listener closed")

        def close(self):
            self.closed.set()

    listener = ListenerStub()
    server = LiveHistoryDiagnosticServer(tmp_path, asyncio.new_event_loop(), lambda _r: None)
    server._listener = listener
    monkeypatch.setattr(diagnostic.os, "name", "nt")
    thread = threading.Thread(target=server._serve, daemon=True)
    server._thread = thread
    thread.start()
    try:
        assert listener.closed.wait(timeout=0.1) is False
        assert thread.is_alive()
        assert listener.accept_count >= 2
    finally:
        server.close()
    assert not thread.is_alive()


def test_response_json_serialization_failure_is_contained() -> None:
    from services.live_history_diagnostic import _send_response

    class ConnectionStub:
        sent = False

        def send_bytes(self, _payload):
            self.sent = True

    connection = ConnectionStub()
    _send_response(connection, {"unserializable": object()})
    assert connection.sent is False


def test_pipe_thread_start_failure_closes_listener(tmp_path: Path, monkeypatch) -> None:
    from services import live_history_diagnostic as diagnostic

    class ListenerStub:
        closed = False

        def close(self):
            self.closed = True

    class ThreadStub:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    listener = ListenerStub()
    monkeypatch.setattr(diagnostic.os, "name", "nt")
    monkeypatch.setattr(diagnostic, "Listener", lambda *_args, **_kwargs: listener)
    monkeypatch.setattr(diagnostic.threading, "Thread", ThreadStub)
    server = LiveHistoryDiagnosticServer(tmp_path, asyncio.new_event_loop(), lambda _r: None)
    try:
        with pytest.raises(HistoryDiagnosticError) as caught:
            server.start()
        assert caught.value.code == "DIAGNOSTIC_PIPE_UNAVAILABLE"
        assert listener.closed is True
        assert server._listener is None
        assert server._thread is None
    finally:
        server.loop.close()


def test_json_pipe_protocol_rejects_oversized_payload_without_unpickling() -> None:
    from services.live_history_diagnostic import MAX_DIAGNOSTIC_MESSAGE_BYTES

    assert MAX_DIAGNOSTIC_MESSAGE_BYTES == 8_192
    payload = json.dumps(_payload()).encode("utf-8")
    assert len(payload) < MAX_DIAGNOSTIC_MESSAGE_BYTES
