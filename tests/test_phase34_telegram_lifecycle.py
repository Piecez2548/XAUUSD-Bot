from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from config.settings import Settings
from domain.events import (
    DomainEvent,
    ErrorPayload,
    EventSeverity,
    EventType,
    RiskSnapshotCreatedPayload,
    SystemStatusPayload,
)
from models.market import MarketSnapshot
from notifications.telegram import TelegramNotifier
from persistence.database import Database
from persistence.repositories import SnapshotRepository
from services.control import TelegramControlService
from services.risk import calculate_risk_snapshot


class FakeLifecycleSupervisor:
    def __init__(self) -> None:
        self.records = {
            "api": SimpleNamespace(state="RUNNING", desired_state="RUNNING"),
            "live": SimpleNamespace(state="RUNNING", desired_state="RUNNING"),
        }

    def status(self):
        return self.records


def _event(event_type: EventType, *, severity: EventSeverity = EventSeverity.INFO) -> DomainEvent:
    if event_type == EventType.RISK_SNAPSHOT_CREATED:
        payload = RiskSnapshotCreatedPayload(
            risk_snapshot_id=uuid4(),
            open_risk_percent=0.0,
            remaining_risk_percent=6.0,
            unbounded_positions=0,
        )
    elif event_type == EventType.SYSTEM_ERROR:
        payload = ErrorPayload(
            error_code="TEST_ERROR",
            message="test",
            recoverable=False,
            component="test",
        )
    else:
        payload = SystemStatusPayload(message="test", component="test", status="test")
    return DomainEvent(
        event_type=event_type,
        source="test",
        severity=severity,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_routine_lifecycle_events_are_coalesced() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(
            enabled=True,
            bot_token="token",
            chat_id="chat",
            client=client,
            max_attempts=1,
        )
        for event in (
            _event(EventType.MT5_CONNECTED),
            _event(EventType.RISK_SNAPSHOT_CREATED),
            _event(EventType.SYSTEM_LIVE_STARTED),
            _event(EventType.SYSTEM_LIVE_STOPPED),
        ):
            await notifier.handle(event)
    assert attempts == 0


@pytest.mark.asyncio
async def test_actionable_events_remain_independently_alertable() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(
            enabled=True,
            bot_token="token",
            chat_id="chat",
            client=client,
            max_attempts=1,
        )
        await notifier.handle(_event(EventType.MT5_DISCONNECTED))
        await notifier.handle(
            _event(EventType.RISK_SNAPSHOT_CREATED, severity=EventSeverity.WARNING)
        )
        await notifier.handle(_event(EventType.SYSTEM_ERROR, severity=EventSeverity.ERROR))
    assert attempts == 3


def _service(tmp_path, *, supervisor: FakeLifecycleSupervisor | None = None):
    database = Database(f"sqlite:///{(tmp_path / 'lifecycle.db').as_posix()}")
    database.create_schema()
    settings = Settings(
        telegram_enabled=True,
        telegram_control_enabled=True,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        telegram_allowed_chat_ids=("chat",),
        telegram_allowed_user_ids=("operator",),
        forward_shadow_enabled=False,
    )
    service = TelegramControlService(
        settings,
        tmp_path,
        database=database,
        supervisor=supervisor or FakeLifecycleSupervisor(),
        logger=logging.getLogger("phase34-lifecycle"),
    )
    return service, database


def _healthy_status() -> dict[str, object]:
    return {
        "control": "CONNECTED",
        "supervisor": "CONNECTED",
        "api": "RUNNING",
        "live": "RUNNING",
        "mt5": "CONNECTED",
        "forward_shadow": "CONNECTED",
        "execution": {"real_money_execution": "DISABLED"},
    }


@pytest.mark.parametrize(
    ("command", "headline"),
    [
        ("start", "SYSTEM STARTED"),
        ("stop", "SYSTEM STOPPED"),
        ("restart", "SYSTEM RESTARTED"),
    ],
)
def test_web_lifecycle_sends_exactly_one_summary(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command: str, headline: str
) -> None:
    service, database = _service(tmp_path)
    try:
        boundary = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)
        status = _healthy_status()
        if command == "stop":
            status["live"] = "STOPPED"
        monkeypatch.setattr(service, "_operator_status_payload", lambda: status)
        monkeypatch.setattr(
            service,
            "_snapshot_context",
            lambda **_kwargs: (
                SimpleNamespace(open_risk_percent=0.0, max_aggregate_risk_percent=6.0),
                boundary,
                "LIVE",
                (),
                None,
            ),
        )
        service._startup_started_at = boundary

        async def fake_start() -> str:
            return service._lifecycle_summary("start", startup_boundary=boundary)

        async def fake_stop() -> str:
            return service._lifecycle_summary("stop")

        async def fake_restart() -> str:
            return service._lifecycle_summary("restart", startup_boundary=boundary)

        monkeypatch.setattr(service, "_start_infrastructure_locked", fake_start)
        monkeypatch.setattr(service, "_stop_infrastructure_locked", fake_stop)
        monkeypatch.setattr(service, "_restart_infrastructure_locked", fake_restart)
        sent: list[tuple[str, str]] = []

        async def send(chat_id: str, message: str) -> None:
            sent.append((chat_id, message))

        monkeypatch.setattr(service, "send_message", send)
        response = asyncio.run(
            service._handle_ipc_request(
                {"command": command, "operation_id": str(uuid4()), "actor": "web"}
            )
        )
        assert response["ok"] is True
        assert headline in str(response["message"])
        assert len(sent) == 1
        assert sent[0] == ("chat", response["message"])
        if command != "stop":
            assert "Risk: 0.00% / 6.00%" in str(response["message"])
        assert "Real-money trading: Disabled" in str(response["message"])
    finally:
        database.dispose()


@pytest.mark.parametrize("command", ["start", "stop", "restart"])
def test_failed_lifecycle_does_not_send_success_summary(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    service, database = _service(tmp_path)
    try:
        monkeypatch.setattr(
            service,
            "_operator_status_payload",
            lambda: {**_healthy_status(), "supervisor": "DEGRADED", "live": "STOPPED"},
        )

        async def failed() -> str:
            return "🟠 OPERATION FAILED"

        monkeypatch.setattr(service, f"_{command}_infrastructure_locked", failed)
        sent: list[str] = []

        async def send(_chat_id: str, message: str) -> None:
            sent.append(message)

        monkeypatch.setattr(service, "send_message", send)
        response = asyncio.run(
            service._handle_ipc_request(
                {"command": command, "operation_id": str(uuid4()), "actor": "web"}
            )
        )
        assert response["ok"] is True
        assert sent == []
        assert "SYSTEM" not in str(response["message"])
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("command", "headline"),
    [
        ("/start", "SYSTEM STARTED"),
        ("/stop", "SYSTEM STOPPED"),
        ("/restart", "SYSTEM RESTARTED"),
    ],
)
def test_telegram_dispatch_uses_the_same_lifecycle_summary(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command: str, headline: str
) -> None:
    service, database = _service(tmp_path)
    try:
        boundary = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)
        status = _healthy_status()
        if command == "/stop":
            status["live"] = "STOPPED"
        monkeypatch.setattr(service, "_operator_status_payload", lambda: status)
        monkeypatch.setattr(
            service,
            "_snapshot_context",
            lambda **_kwargs: (
                SimpleNamespace(open_risk_percent=0.0, max_aggregate_risk_percent=6.0),
                boundary,
                "LIVE",
                (),
                None,
            ),
        )
        service._startup_started_at = boundary

        async def fake_start() -> str:
            return service._lifecycle_summary("start", startup_boundary=boundary)

        async def fake_stop() -> str:
            return service._lifecycle_summary("stop")

        async def fake_restart() -> str:
            return service._lifecycle_summary("restart", startup_boundary=boundary)

        monkeypatch.setattr(service, "_start_infrastructure", fake_start)
        monkeypatch.setattr(service, "_stop_infrastructure", fake_stop)
        monkeypatch.setattr(service, "_restart_infrastructure", fake_restart)
        response = asyncio.run(service._dispatch(command))
        assert headline in response
    finally:
        database.dispose()


def test_telegram_delivery_failure_does_not_change_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, database = _service(tmp_path)
    try:
        monkeypatch.setattr(service, "_operator_status_payload", _healthy_status)
        monkeypatch.setattr(
            service,
            "_snapshot_context",
            lambda **_kwargs: (None, None, "UNKNOWN", (), None),
        )

        async def fake_start() -> str:
            return service._lifecycle_summary("start", startup_boundary=datetime.now(UTC))

        monkeypatch.setattr(service, "_start_infrastructure_locked", fake_start)

        async def fail(_chat_id: str, _message: str) -> None:
            raise RuntimeError("telegram unavailable")

        monkeypatch.setattr(service, "send_message", fail)
        response = asyncio.run(
            service._handle_ipc_request(
                {"command": "start", "operation_id": str(uuid4()), "actor": "web"}
            )
        )
        assert response["ok"] is True
        assert "SYSTEM STARTED" in str(response["message"])
        assert "Risk:" not in str(response["message"])
    finally:
        database.dispose()


@pytest.mark.parametrize("command", ["start", "restart"])
def test_lifecycle_summary_omits_pre_start_risk(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    service, database = _service(tmp_path)
    try:
        boundary = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)
        observed_at = boundary - timedelta(seconds=1)
        monkeypatch.setattr(service, "_operator_status_payload", _healthy_status)

        def snapshot_context(*, minimum_timestamp=None):
            assert minimum_timestamp == boundary
            if observed_at < minimum_timestamp:
                return None, observed_at, "LIVE", (), None
            return (
                SimpleNamespace(open_risk_percent=0.0, max_aggregate_risk_percent=6.0),
                observed_at,
                "LIVE",
                (),
                None,
            )

        monkeypatch.setattr(service, "_snapshot_context", snapshot_context)

        async def lifecycle() -> str:
            return service._lifecycle_summary(command, startup_boundary=boundary)

        monkeypatch.setattr(service, f"_{command}_infrastructure_locked", lifecycle)
        sent: list[str] = []

        async def send(_chat_id: str, message: str) -> None:
            sent.append(message)

        monkeypatch.setattr(service, "send_message", send)
        response = asyncio.run(
            service._handle_ipc_request(
                {"command": command, "operation_id": str(uuid4()), "actor": "web"}
            )
        )
        assert response["ok"] is True
        assert len(sent) == 1
        assert "Risk:" not in sent[0]
        assert "UNKNOWN / UNKNOWN" not in sent[0]
    finally:
        database.dispose()


@pytest.mark.parametrize("freshness", ["STATE_SYNC_PENDING", "STALE", "UNKNOWN"])
@pytest.mark.parametrize("command", ["start", "restart"])
def test_lifecycle_summary_omits_non_authoritative_risk(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    freshness: str,
) -> None:
    service, database = _service(tmp_path)
    try:
        boundary = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)
        monkeypatch.setattr(service, "_operator_status_payload", _healthy_status)
        monkeypatch.setattr(
            service,
            "_snapshot_context",
            lambda *, minimum_timestamp: (
                SimpleNamespace(open_risk_percent=0.0, max_aggregate_risk_percent=6.0),
                boundary,
                freshness,
                (),
                None,
            ),
        )

        async def lifecycle() -> str:
            return service._lifecycle_summary(command, startup_boundary=boundary)

        monkeypatch.setattr(service, f"_{command}_infrastructure_locked", lifecycle)
        sent: list[str] = []

        async def send(_chat_id: str, message: str) -> None:
            sent.append(message)

        monkeypatch.setattr(service, "send_message", send)
        response = asyncio.run(
            service._handle_ipc_request(
                {"command": command, "operation_id": str(uuid4()), "actor": "web"}
            )
        )
        assert response["ok"] is True
        assert len(sent) == 1
        assert "Risk:" not in sent[0]
        assert "UNKNOWN / UNKNOWN" not in sent[0]
    finally:
        database.dispose()


def test_snapshot_context_rejects_pre_start_risk_from_lifecycle_context(
    tmp_path, model_parts
) -> None:
    service, database = _service(tmp_path)
    try:
        account, symbol, tick, candles, _position = model_parts
        boundary = datetime.now(UTC)
        old_snapshot = MarketSnapshot(
            account=account,
            symbol=symbol,
            tick=tick,
            candles=candles,
            positions=(),
            generated_at=boundary - timedelta(seconds=1),
        )
        repository = SnapshotRepository(database)
        repository.persist(
            old_snapshot,
            calculate_risk_snapshot(
                old_snapshot,
                max_trade_risk_percent=2,
                max_aggregate_risk_percent=6,
            ),
        )

        risk, _observed_at, _freshness, _rows, _snapshot_id = service._snapshot_context(
            minimum_timestamp=boundary
        )
        assert risk is None

        new_snapshot = old_snapshot.model_copy(
            update={"generated_at": boundary + timedelta(seconds=1)}
        )
        repository.persist(
            new_snapshot,
            calculate_risk_snapshot(
                new_snapshot,
                max_trade_risk_percent=2,
                max_aggregate_risk_percent=6,
            ),
        )
        risk, _observed_at, freshness, _rows, _snapshot_id = service._snapshot_context(
            minimum_timestamp=boundary
        )
        assert risk is not None
        assert freshness == "LIVE"
    finally:
        database.dispose()
