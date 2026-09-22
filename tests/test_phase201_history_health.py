from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.app import create_app
from api.realtime import RealtimeHub
from config.settings import Settings
from events.bus import EventBus
from persistence.database import Database
from persistence.orm import SystemHealthRecord
from persistence.repositories import SystemHealthRepository
from services.control import TelegramControlService
from services.live import LiveDataEngine
from services.supervisor import ProcessSupervisor
from services.worker_health import derive_worker_state, worker_health_ttl


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'history-health.db').as_posix()}")
    database.create_schema()
    return database


def _latest(database: Database) -> SystemHealthRecord | None:
    with database.session() as session:
        return session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "worker:history")
            .order_by(SystemHealthRecord.timestamp.desc())
            .limit(1)
        )


def test_repeated_success_cycles_keep_authoritative_history_connected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        engine = LiveDataEngine(
            Settings(),
            database,
            EventBus(logging.getLogger("phase201")),
            logger=logging.getLogger("phase201"),
        )
        engine._record_worker_success("history")
        engine._record_worker_success("history")
        row = _latest(database)
        assert row is not None
        assert derive_worker_state(
            status=row.status,
            timestamp=row.timestamp,
            interval_seconds=30,
        ) == "CONNECTED"
        assert row.metadata_json["heartbeat_at"] == row.metadata_json["last_success_at"]
    finally:
        database.dispose()


def test_normal_polling_gap_is_not_unknown_and_stale_policy_is_bounded(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repository = SystemHealthRepository(database)
        repository.record("worker:history", "CONNECTED", message="heartbeat")
        row = _latest(database)
        assert row is not None
        now = datetime.now(UTC)
        assert derive_worker_state(
            status=row.status,
            timestamp=now - timedelta(seconds=worker_health_ttl(30) - 1),
            interval_seconds=30,
            now=now,
        ) == "CONNECTED"
        assert derive_worker_state(
            status=row.status,
            timestamp=now - timedelta(seconds=worker_health_ttl(30) + 1),
            interval_seconds=30,
            now=now,
        ) == "DEGRADED"
        assert derive_worker_state(
            status=row.status,
            timestamp=now - timedelta(seconds=worker_health_ttl(30) * 2 + 1),
            interval_seconds=30,
            now=now,
        ) == "UNKNOWN"
    finally:
        database.dispose()


def test_failure_and_recovery_are_exposed_identically_to_api_and_telegram(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        engine = LiveDataEngine(
            Settings(),
            database,
            EventBus(logging.getLogger("phase201")),
            logger=logging.getLogger("phase201"),
        )
        engine._record_worker_failure("history", RuntimeError("broker unavailable"))
        service = TelegramControlService(Settings(), tmp_path, database=database)
        with TestClient(create_app(settings=Settings(), database=database)) as client:
            assert (
                client.get("/api/system/health").json()["services"]["history_worker"]
                == "DEGRADED"
            )
            assert "Worker:History  DEGRADED" in service._health()
        engine._record_worker_success("history")
        with TestClient(create_app(settings=Settings(), database=database)) as client:
            payload = client.get("/api/system/health").json()
            assert payload["services"]["history_worker"] == "CONNECTED"
            workers = client.get("/api/live/status").json()["workers"]
            assert (
                next(item for item in workers if item["name"] == "history")["state"]
                == "CONNECTED"
            )
        updates = RealtimeHub(database, history_interval_seconds=30)._fetch_new_health()
        history_updates = [item for item in updates if item["component"] == "worker:history"]
        assert history_updates[-1]["status"] == "CONNECTED"
        assert "Worker:History  CONNECTED" in service._health()
    finally:
        database.dispose()


def test_second_supervisor_adopts_existing_live_command(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    first = ProcessSupervisor(tmp_path)
    record = first.start_component("live", command)
    second = ProcessSupervisor(tmp_path)
    adopted = second.start_component("live", command)
    try:
        assert adopted.pid == record.pid
        assert adopted.state == "RUNNING"
    finally:
        first.stop_component("live")
