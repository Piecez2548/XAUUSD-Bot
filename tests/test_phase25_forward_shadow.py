import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from api.app import create_app
from config.settings import Settings
from persistence.database import Database
from persistence.orm import SystemHealthRecord
from services.forward_shadow import (
    EXPECTED_PAIR_ZONE_FILE_SHA256,
    ForwardShadowWorker,
    forward_cost_r,
    forward_health,
    forward_performance_rows,
)
from services.shadow_outcome import EvaluationPolicy, evaluate_decision


def _database(tmp_path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'forward.db').as_posix()}")
    database.create_schema()
    return database


def _trade(timestamp: datetime, *, side: str = "BUY", state: str = "TP", net: float = 1.0):
    return SimpleNamespace(
        timestamp=timestamp,
        side=side,
        state=state,
        gross_r=1.0 if state != "EXPIRED" else 0.25,
        net_r=net,
        spread_points=20.0,
        total_cost_r=0.1,
    )


def test_forward_cost_policy_uses_spread_and_slippage() -> None:
    assert forward_cost_r(
        side="BUY", entry=100.0, stop=99.0, spread_points=20.0, point=0.01,
        entry_slippage_points=0.5, exit_slippage_points=0.5, commission_r=0.02,
    ) == pytest.approx(0.23)


def test_forward_performance_separates_expiry_and_keeps_repeated_cycles_deterministic() -> None:
    start = datetime(2026, 9, 22, tzinfo=UTC)
    rows = [_trade(start, net=0.9), _trade(start + timedelta(minutes=5), state="EXPIRED", net=0.15)]
    first = forward_performance_rows(rows)
    second = forward_performance_rows(list(reversed(rows)))
    assert first == second
    assert first["signals"] == 2
    assert first["combined"]["net_total_r"] == pytest.approx(1.05)
    assert first["expired_only"]["signals"] == 1
    assert first["tp_sl_only"]["signals"] == 1


def test_outcome_is_future_only_and_marks_same_candle_ambiguity() -> None:
    timestamp = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    decision = SimpleNamespace(
        decision="BUY", entry_price=100.0, stop_loss=99.0, take_profit=101.0,
        m5_candle_timestamp=timestamp,
    )
    same = SimpleNamespace(
        id=1,
        timestamp=timestamp, raw_timestamp=int(timestamp.timestamp()), open=100.0,
        high=101.5, low=98.5, close=100.0, tick_volume=1, spread=20, real_volume=0,
    )
    future = SimpleNamespace(
        id=2,
        timestamp=timestamp + timedelta(minutes=5),
        raw_timestamp=int((timestamp + timedelta(minutes=5)).timestamp()),
        open=100.0, high=101.5, low=98.5, close=100.0, tick_volume=1, spread=20, real_volume=0,
    )
    same_result = evaluate_decision(decision, [same], policy=EvaluationPolicy(horizon_bars=1))
    future_result = evaluate_decision(decision, [future], policy=EvaluationPolicy(horizon_bars=1))
    assert same_result["terminal_status"] == "PENDING"
    assert future_result["terminal_status"] == "AMBIGUOUS"


def test_forward_activation_persists_boundary_and_restart_reuses_session(tmp_path) -> None:
    database = _database(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'forward.db').as_posix()}",
        forward_shadow_enabled=True,
        trading_symbol="XAUUSD",
    )
    worker = ForwardShadowWorker(settings, database, logger=logging.getLogger("forward-test"))
    worker._initialize_session()
    assert worker.session is not None
    activation = worker.session.started_at
    assert worker.session.execution_allowed is False
    worker_again = ForwardShadowWorker(settings, database, logger=logging.getLogger("forward-test"))
    worker_again._initialize_session()
    assert worker_again.session is not None
    assert worker_again.session.session_id == worker.session.session_id
    assert worker_again.session.started_at == activation
    assert worker_again.session.strategy_config_hash == EXPECTED_PAIR_ZONE_FILE_SHA256
    database.dispose()


def test_forward_health_disabled_and_connected_heartbeat(tmp_path) -> None:
    database = _database(tmp_path)
    disabled = Settings(database_url=f"sqlite:///{(tmp_path / 'forward.db').as_posix()}")
    assert forward_health(database, disabled)["state"] == "DISABLED"
    enabled = Settings(
        database_url=f"sqlite:///{(tmp_path / 'forward.db').as_posix()}",
        forward_shadow_enabled=True,
    )
    worker = ForwardShadowWorker(enabled, database, logger=logging.getLogger("forward-test"))
    worker._record_health("CONNECTED", "test heartbeat", force=True)
    worker._record_health("CONNECTED", "second test heartbeat", force=True)
    assert forward_health(database, enabled)["state"] == "CONNECTED"
    database.dispose()


def test_forward_health_failure_staleness_and_recovery_are_truthful(tmp_path) -> None:
    database = _database(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'forward.db').as_posix()}",
        forward_shadow_enabled=True,
        live_history_interval_seconds=30.0,
    )
    now = datetime.now(UTC)
    with database.session() as session:
        session.add(SystemHealthRecord(
            component="worker:forward_shadow", timestamp=now - timedelta(seconds=70),
            status="DEGRADED", message="temporary failure",
            metadata_json={"execution_allowed": False},
        ))
    assert forward_health(database, settings)["state"] == "DEGRADED"
    with database.session() as session:
        session.execute(delete(SystemHealthRecord))
        session.add(SystemHealthRecord(
            component="worker:forward_shadow", timestamp=now - timedelta(seconds=250),
            status="DEGRADED", message="stale failure", metadata_json={"execution_allowed": False},
        ))
    assert forward_health(database, settings)["state"] == "UNKNOWN"
    with database.session() as session:
        session.add(SystemHealthRecord(
            component="worker:forward_shadow", timestamp=datetime.now(UTC),
            status="CONNECTED", message="recovered", metadata_json={"execution_allowed": False},
        ))
    assert forward_health(database, settings)["state"] == "CONNECTED"
    with TestClient(create_app(settings=settings, database=database)) as client:
        api_health = client.get("/api/forward/health").json()
        system_health = client.get("/api/system/health").json()
        assert api_health["state"] == "CONNECTED"
        assert system_health["services"]["forward_shadow_worker"] == "CONNECTED"
    database.dispose()


def test_forward_api_returns_read_only_consistent_empty_state(tmp_path) -> None:
    database = _database(tmp_path)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'forward.db').as_posix()}")
    with TestClient(create_app(settings=settings, database=database)) as client:
        health = client.get("/api/forward/health")
        performance = client.get("/api/forward/performance")
        assert health.status_code == 200
        assert health.json()["state"] == "DISABLED"
        assert performance.status_code == 200
        assert performance.json()["execution_allowed"] is False
        assert performance.json()["signals"] == 0
        assert client.get("/api/forward/session").json() is None
    database.dispose()
