from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

import services.control as control_module
from config.settings import Settings
from persistence.database import Database
from persistence.orm import ControlAuditRecord, SystemHealthRecord
from services.control import TelegramControlService
from services.supervisor import ProcessSupervisor


@pytest.fixture
def control_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


def _settings() -> Settings:
    return Settings(
        telegram_enabled=True,
        telegram_bot_token="token-never-returned",
        telegram_chat_id="chat",
        telegram_control_enabled=True,
        telegram_allowed_chat_ids=("123",),
        telegram_allowed_user_ids=("456",),
    )


def test_control_denies_by_default_and_audits(control_database: Database, tmp_path: Path) -> None:
    service = TelegramControlService(
        _settings(),
        tmp_path,
        database=control_database,
        supervisor=ProcessSupervisor(tmp_path),
        logger=logging.getLogger("test-control"),
    )
    response = asyncio.run(
        service.handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 999, "type": "private"},
                    "from": {"id": 456},
                    "text": "/status",
                },
            }
        )
    )
    assert response == "Unauthorized control identity."
    with control_database.session() as session:
        audit = session.scalar(select(ControlAuditRecord))
        assert audit is not None
        assert audit.authorized is False
        assert audit.result == "UNAUTHORIZED"


def test_authorized_help_and_duplicate_update_are_safe(
    control_database: Database, tmp_path: Path
) -> None:
    service = TelegramControlService(
        _settings(),
        tmp_path,
        database=control_database,
        supervisor=ProcessSupervisor(tmp_path),
    )
    update = {
        "update_id": 7,
        "message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "/help"},
    }
    response = asyncio.run(service.handle_update(update))
    assert response is not None and "/start" in response
    assert "token-never-returned" not in response
    assert asyncio.run(service.handle_update(update)) is None


@pytest.mark.parametrize(
    "command",
    [
        "/status",
        "/health",
        "/account",
        "/market",
        "/positions",
        "/risk",
        "/logs",
        "/shadowhealth",
        "/help",
    ],
)
def test_read_only_commands_are_available(
    command: str, control_database: Database, tmp_path: Path
) -> None:
    service = TelegramControlService(
        _settings(),
        tmp_path,
        database=control_database,
        supervisor=ProcessSupervisor(tmp_path),
    )
    response = asyncio.run(
        service.handle_update(
            {
                "update_id": hash(command) & 0xFFFF,
                "message": {
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 456},
                    "text": command,
                },
            }
        )
    )
    assert response is not None
    assert "token-never-returned" not in response


def test_authorized_stop_is_graceful_and_read_only(
    control_database: Database, tmp_path: Path
) -> None:
    service = TelegramControlService(
        _settings(),
        tmp_path,
        database=control_database,
        supervisor=ProcessSupervisor(tmp_path),
    )
    response = asyncio.run(
        service.handle_update(
            {
                "update_id": 88,
                "message": {
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 456},
                    "text": "/stop",
                },
            }
        )
    )
    assert response is not None
    assert "Broker positions were NOT modified" in response
    assert "การเทรดเงินจริง: ปิดอยู่" in response


@pytest.mark.asyncio
async def test_control_delivery_updates_sanitized_telegram_health(
    control_database: Database, tmp_path: Path
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "token-never-returned" in str(request.url)
        return httpx.Response(200, request=request, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TelegramControlService(
            _settings(), tmp_path, database=control_database, client=client
        )
        await service.send_message("123", "safe response")

    with control_database.session() as session:
        health = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "telegram")
            .order_by(SystemHealthRecord.timestamp.desc())
            .limit(1)
        )
        assert health is not None
        assert health.status == "CONNECTED"
        assert health.metadata_json == {"outcome": "success", "consecutive_failures": 0}


def test_process_supervisor_prevents_duplicate_and_stops(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path)
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    first = supervisor.start_component("test", command)
    second = supervisor.start_component("test", command)
    assert first.pid == second.pid
    assert second.state == "RUNNING"
    stopped = supervisor.stop_component("test")
    assert stopped.state == "STOPPED"


def test_stale_supervisor_lock_is_recoverable(tmp_path: Path) -> None:
    lock = tmp_path / "supervisor.lock"
    lock.write_text("999999999", encoding="utf-8")
    supervisor = ProcessSupervisor(tmp_path, lock_path=lock)
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    record = supervisor.start_component("test", command)
    assert record.state == "RUNNING"
    supervisor.stop_component("test")


def test_crash_recovery_is_bounded(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path, max_restarts=1, restart_window_seconds=60)
    command = [sys.executable, "-c", "raise SystemExit(3)"]
    supervisor.start_component("test", command)
    changed = ()
    for _ in range(20):
        asyncio.run(asyncio.sleep(0.05))
        changed = supervisor.monitor_once({"test": command})
        if changed:
            break
    assert changed
    assert changed[0].restart_count == 1
    final = ()
    for _ in range(20):
        asyncio.run(asyncio.sleep(0.05))
        final = supervisor.monitor_once({"test": command})
        if final:
            break
    assert final and final[0].state == "ERROR"


def test_start_remains_incomplete_until_bounded_verification_succeeds(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSupervisor:
        def start_component(self, component, command):
            return SimpleNamespace(component=component, state="RUNNING")

        def status(self):
            return {
                "api": SimpleNamespace(state="RUNNING"),
                "live": SimpleNamespace(state="RUNNING"),
            }

    service = TelegramControlService(
        _settings(),
        tmp_path,
        database=control_database,
        supervisor=FakeSupervisor(),
    )
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)

    async def incomplete_verification():
        return {
            "complete": False,
            "checks": {
                "api": "OK",
                "live_runtime": "UNKNOWN",
                "mt5": "UNKNOWN",
                "snapshot": "UNKNOWN",
                "database": "CONNECTED",
            },
        }

    monkeypatch.setattr(service, "_wait_for_startup_verification", incomplete_verification)
    response = asyncio.run(service._start_infrastructure_locked())
    assert response.startswith("🟠 START INCOMPLETE")
    assert "MONITORING STARTED" not in response
