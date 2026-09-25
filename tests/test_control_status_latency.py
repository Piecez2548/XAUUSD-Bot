from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from config.settings import Settings
from persistence.database import Database
from persistence.orm import SystemHealthRecord
from services.control import TelegramControlService


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


class CountingSupervisor:
    def __init__(self) -> None:
        self.calls = 0
        self.records = {
            "api": SimpleNamespace(state="RUNNING", desired_state="RUNNING"),
            "live": SimpleNamespace(
                state="RUNNING",
                desired_state="RUNNING",
                process_identities=(),
            ),
        }

    def status(self):
        self.calls += 1
        return self.records


def _service(database: Database, tmp_path: Path, supervisor) -> TelegramControlService:
    return TelegramControlService(
        Settings(
            telegram_enabled=True,
            telegram_bot_token="test-token",
            telegram_chat_id="123",
            forward_shadow_enabled=False,
        ),
        tmp_path,
        database=database,
        supervisor=supervisor,
        mt5_bootstrap=SimpleNamespace(
            background_status={"enabled": False, "state": "DISABLED", "pid": None}
        ),
    )


def test_status_request_reuses_one_supervisor_snapshot_and_preserves_contract(
    tmp_path: Path,
) -> None:
    db = Database.for_test(f"sqlite:///{(tmp_path / 'status.db').as_posix()}")
    db.create_test_schema()
    supervisor = CountingSupervisor()
    service = _service(db, tmp_path, supervisor)
    now = datetime.now(UTC)
    with db.session() as session:
        for component in ("live_runtime", "mt5", "telegram"):
            session.add(
                SystemHealthRecord(
                    component=component,
                    status="CONNECTED",
                    timestamp=now,
                    latency_ms=None,
                    message=None,
                    metadata_json=None,
                )
            )

    response = asyncio.run(
        service._handle_ipc_request(
            {"command": "status", "operation_id": str(uuid4())}
        )
    )

    assert supervisor.calls == 1
    assert response["ok"] is True
    assert "Supervisor" in response["message"] and "(CONNECTED)" in response["message"]
    assert "Live Engine" in response["message"] and "(RUNNING)" in response["message"]
    assert "API" in response["message"] and "(RUNNING)" in response["message"]
    status = response["status"]
    assert status["control"] == "CONNECTED"
    assert status["supervisor"] == "CONNECTED"
    assert status["api"] == "RUNNING"
    assert status["live"] == "RUNNING"
    assert status["mt5"] == "CONNECTED"
    assert status["database"] == "CONNECTED"
    assert status["telegram"] == "CONNECTED"
    assert status["forward_shadow"] == "DISABLED"
    assert status["strategy"]["pair_zone_state"] == "UNKNOWN"
    assert status["strategy"]["latest_canonical_signal_id"] is None
    assert status["execution"]["demo_kill_switch_armed"] is False
    assert status["execution"]["real_money_execution"] == "DISABLED"
    db.dispose()


def test_missing_and_stale_health_semantics_remain_fail_closed(tmp_path: Path) -> None:
    db = Database.for_test(f"sqlite:///{(tmp_path / 'health-semantics.db').as_posix()}")
    db.create_test_schema()
    service = _service(db, tmp_path, CountingSupervisor())

    assert service._latest_service_state("live_runtime") == "UNKNOWN"
    assert service._telegram_state() == "UNKNOWN"

    with db.session() as session:
        session.add(
            SystemHealthRecord(
                component="live_runtime",
                status="CONNECTED",
                timestamp=datetime.now(UTC) - timedelta(hours=1),
                latency_ms=None,
                message=None,
                metadata_json=None,
            )
        )
        session.add(
            SystemHealthRecord(
                component="mt5",
                status="CONNECTED",
                timestamp=datetime.now(UTC) - timedelta(hours=1),
                latency_ms=None,
                message=None,
                metadata_json=None,
            )
        )

    assert service._latest_service_state("live_runtime") == "DEGRADED"
    assert service._latest_service_state("mt5") == "DEGRADED"
    db.dispose()


def test_health_index_accelerates_large_latest_query_and_migration_replays(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "health-index.db"
    config = _alembic_config(db_path)
    command.upgrade(config, "20260925_0018")
    db = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO system_health "
                "(id, component, status, timestamp, latency_ms, message, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "health-seed",
                    "live_runtime",
                    "CONNECTED",
                    "2026-01-01 00:00:00",
                    None,
                    None,
                    None,
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO system_health "
                "(id, component, status, timestamp, latency_ms, message, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("health-other", "mt5", "CONNECTED", "2026-01-02 00:00:00", None, None, None),
            )
            connection.exec_driver_sql(
                "INSERT INTO system_health "
                "(id, component, status, timestamp, latency_ms, message, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        f"health-{index:05d}",
                        "live_runtime",
                        "CONNECTED" if index % 2 else "DEGRADED",
                        f"2026-01-{(index % 28) + 1:02d} 00:00:{index % 60:02d}",
                        None,
                        None,
                        None,
                    )
                    for index in range(10_000)
                ],
            )

        query = (
            "SELECT id, status, timestamp FROM system_health "
            "WHERE component = 'live_runtime' ORDER BY timestamp DESC LIMIT 1"
        )
        with db.engine.connect() as connection:
            before_row = connection.exec_driver_sql(query).one()
            before_plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + query
            ).all()
        assert before_row[0].startswith("health-")
        before_plan_text = " ".join(str(row[-1]) for row in before_plan)
        assert "ix_system_health_component" in before_plan_text
        assert "TEMP B-TREE" in before_plan_text.upper()

        db.dispose()
        command.upgrade(config, "20260925_0019")
        db = Database(f"sqlite:///{db_path.as_posix()}")
        with db.engine.connect() as connection:
            after_row = connection.exec_driver_sql(query).one()
            after_plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + query
            ).all()
        assert after_row == before_row
        plan_text = " ".join(str(row[-1]) for row in after_plan)
        assert "ix_system_health_component_timestamp" in plan_text
        assert "TEMP B-TREE" not in plan_text.upper()

        with db.engine.connect() as connection:
            existing_indexes = {
                index["name"] for index in inspect(connection).get_indexes("system_health")
            }
            assert {
                "ix_system_health_component",
                "ix_system_health_timestamp",
                "ix_system_health_component_timestamp",
            } <= existing_indexes

        db.dispose()
        command.downgrade(config, "20260925_0018")
        db = Database(f"sqlite:///{db_path.as_posix()}")
        with db.engine.connect() as connection:
            downgraded_indexes = {
                index["name"] for index in inspect(connection).get_indexes("system_health")
            }
            preserved = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM system_health WHERE component = 'live_runtime'"
            ).scalar_one()
            assert "ix_system_health_component_timestamp" not in downgraded_indexes
            assert {
                "ix_system_health_component",
                "ix_system_health_timestamp",
            } <= downgraded_indexes
            assert preserved == 10_001

        db.dispose()
        command.upgrade(config, "20260925_0019")
        db = Database(f"sqlite:///{db_path.as_posix()}")
        with db.engine.connect() as connection:
            final_indexes = {
                index["name"] for index in inspect(connection).get_indexes("system_health")
            }
            assert "ix_system_health_component_timestamp" in final_indexes
            assert connection.exec_driver_sql(
                "SELECT status FROM system_health WHERE id = 'health-seed'"
            ).scalar_one() == "CONNECTED"
    finally:
        db.dispose()
