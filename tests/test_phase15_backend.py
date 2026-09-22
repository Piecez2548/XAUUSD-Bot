from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from analytics.service import AnalyticsService, TradeSample
from api.app import create_app
from api.realtime import RealtimeHub
from config.settings import ConfigurationError, Settings
from domain.events import (
    DomainEvent,
    ErrorPayload,
    EventType,
    SnapshotCreatedPayload,
)
from events.bus import EventBus
from models.market import MarketSnapshot
from notifications.telegram import TelegramNotifier
from notifications.templates import format_telegram_event
from persistence.database import Database
from persistence.orm import CandleRecord, SystemEventRecord, SystemHealthRecord
from persistence.repositories import EventRepository, SnapshotRepository, SystemHealthRepository
from services.backup import BackupError, create_database_backup
from services.risk import calculate_risk_snapshot


@pytest.fixture
def phase15_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'observatory.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


@pytest.fixture
def market_snapshot(model_parts) -> MarketSnapshot:
    account, symbol, tick, candles, position = model_parts
    return MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(position,),
        generated_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )


def test_risk_snapshot_uses_broker_tick_specification(market_snapshot: MarketSnapshot) -> None:
    risk = calculate_risk_snapshot(
        market_snapshot,
        max_trade_risk_percent=2,
        max_aggregate_risk_percent=6,
    )
    assert risk.open_risk_amount == pytest.approx(100)
    assert risk.open_risk_percent == pytest.approx(100 / 10_100 * 100)
    assert risk.remaining_risk_percent == pytest.approx(6 - risk.open_risk_percent)
    assert risk.max_trade_risk_percent == 2
    assert risk.max_aggregate_risk_percent == 6


def test_missing_stop_makes_aggregate_risk_unavailable(market_snapshot: MarketSnapshot) -> None:
    unbounded = market_snapshot.model_copy(
        update={
            "positions": (
                market_snapshot.positions[0].model_copy(update={"stop_loss": 0}),
            )
        }
    )
    risk = calculate_risk_snapshot(
        unbounded,
        max_trade_risk_percent=2,
        max_aggregate_risk_percent=6,
    )
    assert risk.open_risk_percent is None
    assert risk.remaining_risk_percent is None
    assert risk.unbounded_positions_count == 1


def test_snapshot_repository_persists_closed_candles_and_latest_state(
    phase15_database: Database,
    market_snapshot: MarketSnapshot,
) -> None:
    risk = calculate_risk_snapshot(
        market_snapshot,
        max_trade_risk_percent=2,
        max_aggregate_risk_percent=6,
    )
    result = SnapshotRepository(phase15_database).persist(market_snapshot, risk)
    assert result.market_snapshot_id
    with phase15_database.session() as session:
        assert session.scalar(select(func.count()).select_from(CandleRecord)) == 4


@pytest.mark.asyncio
async def test_event_repository_is_idempotent(phase15_database: Database) -> None:
    repository = EventRepository(phase15_database)
    event = DomainEvent(
        event_type=EventType.SYSTEM_STARTED,
        source="test",
    )
    await repository.handle(event)
    await repository.handle(event)
    with phase15_database.session() as session:
        assert session.scalar(select(func.count()).select_from(SystemEventRecord)) == 1


def test_event_payload_contract_rejects_wrong_payload() -> None:
    with pytest.raises(ValidationError, match="requires payload type"):
        DomainEvent(
            event_type=EventType.MARKET_SNAPSHOT_CREATED,
            source="test",
            payload=ErrorPayload(
                error_code="WRONG",
                message="wrong payload",
                recoverable=True,
                component="test",
            ),
        )


def test_event_payload_serialization_is_versioned() -> None:
    event = DomainEvent(
        event_type=EventType.MARKET_SNAPSHOT_CREATED,
        source="test",
        payload=SnapshotCreatedPayload(
            snapshot_id=uuid4(),
            symbol="XAUUSD",
            positions_count=0,
        ),
    )
    serialized = event.model_dump(mode="json")
    assert serialized["schema_version"] == 1
    assert serialized["payload"]["kind"] == "snapshot_created"


@pytest.mark.asyncio
async def test_event_bus_isolates_optional_subscriber_failures() -> None:
    calls: list[str] = []

    async def failing(_event: DomainEvent) -> None:
        raise RuntimeError("optional failure")

    async def succeeding(_event: DomainEvent) -> None:
        calls.append("persisted")

    bus = EventBus(logging.getLogger("test"))
    bus.subscribe("optional", failing)
    bus.subscribe("next", succeeding)
    await bus.publish(DomainEvent(event_type=EventType.SYSTEM_STARTED, source="test"))
    assert calls == ["persisted"]


def test_analytics_core_formulas() -> None:
    trades = [
        TradeSample("1", "BUY", datetime(2026, 1, 1, tzinfo=UTC), 100, 1),
        TradeSample("2", "SELL", datetime(2026, 1, 2, tzinfo=UTC), -50, -0.5),
        TradeSample("3", "BUY", datetime(2026, 1, 3, tzinfo=UTC), 0, 0),
    ]
    summary = AnalyticsService().summary(trades)
    assert summary["win_rate"] == pytest.approx(100 / 3)
    assert summary["net_profit"] == 50
    assert summary["profit_factor"] == 2
    assert summary["expectancy_r"] == pytest.approx(1 / 6)
    assert summary["sharpe_ratio"] is None
    assert summary["ratio_sample_size"] == 3


def test_confidence_buckets_do_not_invent_samples() -> None:
    trades = [
        TradeSample(
            "1",
            "BUY",
            datetime(2026, 1, 1, tzinfo=UTC),
            100,
            1,
            confidence=0.75,
        )
    ]
    buckets = AnalyticsService().confidence_calibration(trades)
    target = next(bucket for bucket in buckets if bucket["bucket"] == "70-80%")
    empty = next(bucket for bucket in buckets if bucket["bucket"] == "80-90%")
    assert target["trade_count"] == 1
    assert target["win_rate"] == 100
    assert empty["trade_count"] == 0
    assert empty["win_rate"] is None


def test_empty_database_api_is_honest_and_secret_free(
    phase15_database: Database,
) -> None:
    settings = Settings(
        telegram_enabled=True,
        telegram_bot_token="super-secret-token",
        telegram_chat_id="private-chat",
    )
    with TestClient(create_app(settings=settings, database=phase15_database)) as client:
        assert client.get("/api/account").json() is None
        assert client.get("/api/symbol").json() is None
        assert client.get("/api/positions").json() == []
        assert client.get("/api/trades").json() == []
        assert client.get("/api/performance/account-curve").json() == []
        health = client.get("/api/system/health").json()
        assert health["database"] == "CONNECTED"
        assert health["services"]["mt5"] == "UNKNOWN"
        assert health["services"]["telegram"] == "UNKNOWN"
        assert health["services"]["ai_engine"] == "PLANNED"
        assert health["services"]["trade_execution"] == "DISABLED"
        public = client.get("/api/config/public").text
        assert "super-secret-token" not in public
        assert "private-chat" not in public


def test_account_curve_uses_real_snapshots(
    phase15_database: Database,
    market_snapshot: MarketSnapshot,
) -> None:
    risk = calculate_risk_snapshot(
        market_snapshot,
        max_trade_risk_percent=2,
        max_aggregate_risk_percent=6,
    )
    SnapshotRepository(phase15_database).persist(market_snapshot, risk)
    with TestClient(create_app(database=phase15_database)) as client:
        points = client.get("/api/performance/account-curve").json()
    assert points[0]["equity"] == 10_100
    assert points[0]["balance"] == 10_000
    assert points[0]["drawdown_percent"] == 0


@pytest.mark.asyncio
async def test_telegram_retries_then_succeeds_without_leaking_token() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500 if attempts == 1 else 200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(
            enabled=True,
            bot_token="secret",
            chat_id="chat",
            client=client,
            sleep=lambda _seconds: _completed_sleep(),
        )
        assert await notifier.send("test") is True
    assert attempts == 2


def test_telegram_health_is_disabled_when_integration_is_disabled(
    phase15_database: Database,
) -> None:
    app = create_app(settings=Settings(telegram_enabled=False), database=phase15_database)
    with TestClient(app) as client:
        health = client.get("/api/system/health").json()
    assert health["services"]["telegram"] == "DISABLED"


def test_telegram_health_is_unknown_before_any_verified_delivery(
    phase15_database: Database,
) -> None:
    settings = Settings(telegram_enabled=True, telegram_bot_token="secret", telegram_chat_id="chat")
    with TestClient(create_app(settings=settings, database=phase15_database)) as client:
        health = client.get("/api/system/health").json()
    assert health["services"]["telegram"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_telegram_health_persists_connected_after_success(
    phase15_database: Database,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    settings = Settings(
        telegram_enabled=True,
        telegram_bot_token="super-secret",
        telegram_chat_id="chat-123",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(
            enabled=True,
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            client=client,
            health_reporter=SystemHealthRepository(phase15_database),
        )
        assert await notifier.send_test() is True

    with TestClient(create_app(settings=settings, database=phase15_database)) as client:
        response = client.get("/api/system/health")
    assert response.json()["services"]["telegram"] == "CONNECTED"
    assert "super-secret" not in response.text
    assert "chat-123" not in response.text


@pytest.mark.asyncio
async def test_telegram_recoverable_failures_degrade_then_error_and_success_recovers(
    phase15_database: Database,
) -> None:
    async def temporary_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    health_repository = SystemHealthRepository(phase15_database)
    for expected in ("DEGRADED", "DEGRADED", "ERROR"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(temporary_failure)) as client:
            notifier = TelegramNotifier(
                enabled=True,
                bot_token="secret",
                chat_id="chat",
                max_attempts=1,
                client=client,
                health_reporter=health_repository,
            )
            assert await notifier.send("test") is False
        with phase15_database.session() as session:
            latest = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "telegram")
                .order_by(SystemHealthRecord.timestamp.desc(), SystemHealthRecord.id.desc())
                .limit(1)
            )
            assert latest is not None
            assert latest.status == expected

    async def success(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
        notifier = TelegramNotifier(
            enabled=True,
            bot_token="secret",
            chat_id="chat",
            client=client,
            health_reporter=health_repository,
        )
        assert await notifier.send("test") is True

    with phase15_database.session() as session:
        latest = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "telegram")
            .order_by(SystemHealthRecord.timestamp.desc(), SystemHealthRecord.id.desc())
            .limit(1)
        )
        assert latest is not None
        assert latest.status == "CONNECTED"


@pytest.mark.asyncio
async def test_telegram_unrecoverable_failure_is_error_and_realtime_payload_is_sanitized(
    phase15_database: Database,
) -> None:
    async def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    settings = Settings(
        telegram_enabled=True,
        telegram_bot_token="do-not-leak",
        telegram_chat_id="private-chat",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        notifier = TelegramNotifier(
            enabled=True,
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            client=client,
            health_reporter=SystemHealthRepository(phase15_database),
        )
        assert await notifier.send("test") is False

    with TestClient(create_app(settings=settings, database=phase15_database)) as client:
        assert client.get("/api/system/health").json()["services"]["telegram"] == "ERROR"
    updates = RealtimeHub(phase15_database)._fetch_new_health()
    update = next(item for item in updates if item["component"] == "telegram")
    assert update["status"] == "ERROR"
    serialized = str(update)
    assert "do-not-leak" not in serialized
    assert "private-chat" not in serialized


async def _completed_sleep() -> None:
    return None


def test_telegram_template_contains_event_audit_fields() -> None:
    event = DomainEvent(event_type=EventType.SYSTEM_STARTED, source="observatory")
    message = format_telegram_event(event)
    assert "SYSTEM STARTED" in message
    assert str(event.event_id) in message
    assert "UTC" in message


def test_sqlite_backup_is_restorable(
    phase15_database: Database,
    tmp_path: Path,
) -> None:
    source_url = phase15_database.database_url
    backup = create_database_backup(
        source_url,
        tmp_path / "backups",
        project_root=tmp_path,
        now=datetime(2026, 9, 21, tzinfo=UTC),
    )
    restored = Database(f"sqlite:///{backup.as_posix()}")
    try:
        assert restored.healthcheck()
        with restored.session() as session:
            assert session.scalar(select(func.count()).select_from(SystemEventRecord)) == 0
    finally:
        restored.dispose()


def test_memory_database_backup_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="file-based SQLite"):
        create_database_backup(
            "sqlite:///:memory:",
            tmp_path / "backups",
            project_root=tmp_path,
        )


def test_risk_policy_configuration_is_validated() -> None:
    with pytest.raises(ConfigurationError, match="cannot exceed"):
        Settings(max_trade_risk_percent=7, max_aggregate_risk_percent=6)
