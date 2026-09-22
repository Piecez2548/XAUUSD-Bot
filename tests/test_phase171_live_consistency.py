from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.app import create_app
from config.settings import Settings
from events.bus import EventBus
from models.market import MarketSnapshot
from persistence.database import Database
from persistence.orm import (
    MarketSnapshotRecord,
    PositionRecord,
    PositionSnapshotRecord,
    RiskSnapshotRecord,
    SystemHealthRecord,
)
from persistence.repositories import SnapshotRepository, SystemHealthRepository
from services.control import TelegramControlService
from services.live import LiveDataEngine
from services.risk import calculate_risk_snapshot


@pytest.fixture
def consistency_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'consistency.db').as_posix()}")
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
        generated_at=datetime.now(UTC),
    )


def _persist(repository: SnapshotRepository, snapshot: MarketSnapshot):
    return repository.persist(
        snapshot,
        calculate_risk_snapshot(snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6),
    )


def test_verified_empty_positions_remove_stale_current_state(
    consistency_database: Database, market_snapshot: MarketSnapshot
) -> None:
    repository = SnapshotRepository(consistency_database)
    opened = market_snapshot.model_copy(update={"generated_at": datetime.now(UTC)})
    _persist(repository, opened)
    closed = opened.model_copy(
        update={"positions": (), "generated_at": datetime.now(UTC) + timedelta(seconds=1)}
    )
    result = _persist(repository, closed)

    with consistency_database.session() as session:
        active = session.scalar(
            select(func.count())
            .select_from(PositionRecord)
            .where(PositionRecord.closed_observed_at.is_(None))
        )
        risk = session.get(RiskSnapshotRecord, str(result.risk_snapshot_id))
        market = session.get(MarketSnapshotRecord, str(result.market_snapshot_id))
        assert active == 0
        assert risk is not None
        assert market is not None
        assert market.positions_observed_successfully is True
        assert market.open_position_count == 0
        assert risk.open_positions_count == 0
        assert risk.open_risk_percent == 0
        assert risk.market_snapshot_id == str(result.market_snapshot_id)


def test_position_and_risk_share_one_snapshot_id(
    consistency_database: Database, market_snapshot: MarketSnapshot
) -> None:
    result = _persist(
        SnapshotRepository(consistency_database),
        market_snapshot.model_copy(update={"generated_at": datetime.now(UTC)}),
    )
    with consistency_database.session() as session:
        market = session.get(MarketSnapshotRecord, str(result.market_snapshot_id))
        risk = session.get(RiskSnapshotRecord, str(result.risk_snapshot_id))
        assert market is not None and risk is not None
        assert risk.market_snapshot_id == market.id


def test_control_reports_zero_positions_and_live_coherent_risk(
    consistency_database: Database, market_snapshot: MarketSnapshot, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    _persist(
        SnapshotRepository(consistency_database),
        market_snapshot.model_copy(update={"positions": (), "generated_at": now}),
    )
    settings = Settings(
        telegram_enabled=True,
        telegram_bot_token="test-token",
        telegram_chat_id="chat",
        telegram_control_enabled=True,
        telegram_allowed_chat_ids=("123",),
        telegram_allowed_user_ids=("456",),
    )
    service = TelegramControlService(settings, tmp_path, database=consistency_database)
    positions = service._positions()
    risk = service._risk()
    assert "Open positions: 0" in positions
    assert "Freshness: LIVE" in positions
    assert "Bounded positions: 0" in risk
    assert "Unbounded positions: 0" in risk
    assert "Freshness: LIVE" in risk


def test_api_and_dashboard_sources_report_same_empty_live_state(
    consistency_database: Database, market_snapshot: MarketSnapshot
) -> None:
    _persist(
        SnapshotRepository(consistency_database),
        market_snapshot.model_copy(update={"positions": (), "generated_at": datetime.now(UTC)}),
    )
    with TestClient(create_app(settings=Settings(), database=consistency_database)) as client:
        positions = client.get("/api/positions").json()
        position_status = client.get("/api/positions/status").json()
        risk = client.get("/api/risk/current").json()
    assert positions == []
    assert position_status["open_positions"] == 0
    assert position_status["freshness"] == "LIVE"
    assert risk["open_positions_count"] == 0
    assert risk["open_risk_percent"] == 0
    assert risk["freshness"] == "LIVE"
    assert position_status["snapshot_id"] == risk["market_snapshot_id"]


@pytest.mark.asyncio
async def test_live_account_cycle_persists_close_without_worker_race(
    consistency_database: Database, model_parts
) -> None:
    account, symbol, tick, candles, position = model_parts

    class FakeGateway:
        positions = (position,)

        async def call(self, _operation):
            return account, self.positions, symbol

    engine = LiveDataEngine(
        Settings(),
        consistency_database,
        EventBus(logging.getLogger("phase171-test")),
        logger=logging.getLogger("phase171-test"),
        gateway=FakeGateway(),
    )
    engine.state.symbol = symbol.name
    engine.state.specification = symbol
    engine.state.tick = tick
    engine.state.account = account
    engine.state.candles = candles
    await engine._account_loop()
    engine.gateway.positions = ()
    await engine._account_loop()
    with consistency_database.session() as session:
        active = session.scalar(
            select(func.count())
            .select_from(PositionRecord)
            .where(PositionRecord.closed_observed_at.is_(None))
        )
        risk = session.scalar(
            select(RiskSnapshotRecord).order_by(RiskSnapshotRecord.timestamp.desc()).limit(1)
        )
        assert active == 0
        assert risk is not None and risk.open_positions_count == 0


@pytest.mark.asyncio
async def test_successful_live_sync_persists_mt5_connected_health(
    consistency_database: Database, model_parts
) -> None:
    account, symbol, tick, candles, _position = model_parts

    class FakeGateway:
        connected = False
        calls = 0

        async def connect(self):
            self.connected = True

        async def call(self, operation):
            self.calls += 1
            if self.calls == 1:
                return "XAUUSD", ()
            return symbol, tick, account, (), candles

        async def shutdown(self):
            self.connected = False

    engine = LiveDataEngine(
        Settings(),
        consistency_database,
        EventBus(logging.getLogger("phase171-health-test")),
        logger=logging.getLogger("phase171-health-test"),
        gateway=FakeGateway(),
    )
    await engine._synchronize(initial=True)
    with consistency_database.session() as session:
        health = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "mt5")
            .order_by(SystemHealthRecord.timestamp.desc())
            .limit(1)
        )
        assert health is not None and health.status == "CONNECTED"


def test_runtime_heartbeat_missing_or_stale_is_not_healthy(
    consistency_database: Database,
) -> None:
    with TestClient(create_app(settings=Settings(), database=consistency_database)) as client:
        assert client.get("/api/live/status").json()["state"] == "UNKNOWN"
        SystemHealthRepository(consistency_database).record(
            "live_runtime", "CONNECTED", message="old heartbeat"
        )
        with consistency_database.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "live_runtime")
                .order_by(SystemHealthRecord.timestamp.desc())
                .limit(1)
            )
            assert row is not None
            row.timestamp = datetime.now(UTC) - timedelta(minutes=10)
        assert client.get("/api/live/status").json()["state"] == "DEGRADED"
        assert client.get("/api/system/health").json()["services"]["live_engine"] == "DEGRADED"


def test_fresh_runtime_heartbeat_reports_connected(
    consistency_database: Database,
) -> None:
    SystemHealthRepository(consistency_database).record(
        "live_runtime", "CONNECTED", message="heartbeat"
    )
    with TestClient(create_app(settings=Settings(), database=consistency_database)) as client:
        status = client.get("/api/live/status").json()
        assert status["state"] == "CONNECTED"
        assert status["freshness"]["runtime"]["state"] == "LIVE"


def test_mt5_health_failure_then_recovery_is_truthful(
    consistency_database: Database, tmp_path: Path
) -> None:
    health = SystemHealthRepository(consistency_database)
    health.record("mt5", "DISCONNECTED", message="read failed")
    service = TelegramControlService(Settings(), tmp_path, database=consistency_database)
    assert service._latest_service_state("mt5") == "DISCONNECTED"
    health.record("mt5", "CONNECTED", message="verified read")
    assert service._latest_service_state("mt5") == "CONNECTED"


def test_snapshot_persistence_rolls_back_on_required_write_failure(
    consistency_database: Database, market_snapshot: MarketSnapshot
) -> None:
    repository = SnapshotRepository(consistency_database)

    def fail(*_args):
        raise RuntimeError("sensitive detail")

    repository._persist_positions = fail
    with pytest.raises(RuntimeError):
        _persist(repository, market_snapshot)
    with consistency_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MarketSnapshotRecord)) == 0
        assert session.scalar(select(func.count()).select_from(RiskSnapshotRecord)) == 0
        assert session.scalar(select(func.count()).select_from(PositionSnapshotRecord)) == 0


def test_worker_exception_is_sanitized_in_health_state(
    consistency_database: Database,
) -> None:
    engine = LiveDataEngine(
        Settings(),
        consistency_database,
        EventBus(logging.getLogger("phase171-worker-test")),
        logger=logging.getLogger("phase171-worker-test"),
    )
    engine._record_worker_failure("account", RuntimeError("secret credentials"))
    with consistency_database.session() as session:
        health = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "worker:account")
            .order_by(SystemHealthRecord.timestamp.desc())
            .limit(1)
        )
        assert health is not None
        assert health.status == "DEGRADED"
        assert "secret credentials" not in (health.message or "")
        assert health.metadata_json["error_category"] == "RuntimeError"


def test_relative_database_url_is_identical_across_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "application"
    project_root.mkdir()
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    monkeypatch.chdir(first_cwd)
    first = Database("sqlite:///data/shared.db", project_root=project_root)
    monkeypatch.chdir(second_cwd)
    second = Database("sqlite:///data/shared.db", project_root=project_root)
    try:
        assert first.database_url == second.database_url
        assert first.database_identity == second.database_identity
        assert first.database_url.endswith("/application/data/shared.db")
    finally:
        first.dispose()
        second.dispose()


def test_mismatched_latest_rows_surface_sync_pending(
    consistency_database: Database, market_snapshot: MarketSnapshot, tmp_path: Path
) -> None:
    repository = SnapshotRepository(consistency_database)
    opened = market_snapshot.model_copy(update={"generated_at": datetime.now(UTC)})
    _persist(repository, opened)
    empty = opened.model_copy(update={"positions": (), "generated_at": datetime.now(UTC)})
    _persist(repository, empty)
    with consistency_database.session() as session:
        latest_risk = session.scalar(
            select(RiskSnapshotRecord).order_by(RiskSnapshotRecord.timestamp.desc()).limit(1)
        )
        assert latest_risk is not None
        latest_risk.market_snapshot_id = None
        session.flush()
    settings = Settings(telegram_enabled=True, telegram_bot_token="token", telegram_chat_id="chat")
    service = TelegramControlService(settings, tmp_path, database=consistency_database)
    assert "STATE_SYNC_PENDING" in service._risk()
    assert "STATE_SYNC_PENDING" in service._positions()


@pytest.mark.parametrize(
    ("position_type", "open_price", "stop_loss"),
    [(0, 2000, 1990), (1, 2000, 2010)],
)
def test_loss_producing_buy_and_sell_stops_use_broker_tick_economics(
    model_parts, position_type: int, open_price: float, stop_loss: float
) -> None:
    account, symbol, tick, candles, position = model_parts
    candidate = position.model_copy(
        update={
            "type": position_type,
            "type_name": "BUY" if position_type == 0 else "SELL",
            "open_price": open_price,
            "stop_loss": stop_loss,
        }
    )
    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(candidate,),
        generated_at=datetime.now(UTC),
    )
    risk = calculate_risk_snapshot(snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6)
    assert risk.open_risk_amount == pytest.approx(100)
    assert risk.unbounded_positions_count == 0


def test_invalid_or_missing_stop_is_unknown_not_zero(model_parts) -> None:
    account, symbol, tick, candles, position = model_parts
    candidate = position.model_copy(update={"stop_loss": 0})
    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(candidate,),
        generated_at=datetime.now(UTC),
    )
    risk = calculate_risk_snapshot(snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6)
    assert risk.open_risk_percent is None
    assert risk.unbounded_positions_count == 1
