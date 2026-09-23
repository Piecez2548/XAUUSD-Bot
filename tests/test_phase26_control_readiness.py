from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from config.settings import Settings
from persistence.database import Database
from services.control import TelegramControlService


def _verified_api(*, pid: int = 8000, state: str = "RUNNING", desired: str = "RUNNING"):
    return SimpleNamespace(
        pid=pid,
        state=state,
        desired_state=desired,
        process_identities=((pid, "api-create-time"),),
    )


class _StatusSupervisor:
    def __init__(self, api_record) -> None:
        self.api_record = api_record

    def status(self):
        return {"api": self.api_record}


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _service(tmp_path: Path, supervisor) -> TelegramControlService:
    database = Database(f"sqlite:///{(tmp_path / 'readiness.db').as_posix()}")
    database.create_schema()
    service = TelegramControlService(
        Settings(remote_dashboard_mode=True, api_host="127.0.0.1", api_port=8000),
        tmp_path,
        database=database,
        supervisor=supervisor,
    )
    return service


def test_private_mode_unauthenticated_local_api_health_remains_401(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'private-api.db').as_posix()}")
    database.create_schema()
    try:
        with TestClient(
            create_app(
                settings=Settings(remote_dashboard_mode=True),
                database=database,
            )
        ) as client:
            response = client.get("/api/health")
        assert response.status_code == 401
        assert response.json() == {"detail": "Tailscale identity required"}
    finally:
        database.dispose()


def test_verified_api_and_loopback_listener_are_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, _StatusSupervisor(_verified_api()))
    monkeypatch.setattr("services.control.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    try:
        assert asyncio.run(service._api_responsive()) is True
        event = json.loads(
            (tmp_path / "logs" / "supervisor_control_diagnostics.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
    finally:
        service.database.dispose()

    assert event["stage"] == "api_readiness_probe"
    assert event["details"]["readiness_version"] == "phase26-supervisor-socket-readiness-v2"
    assert event["details"]["final_ready"] is True
    assert event["details"]["validated_loopback_host"] == "127.0.0.1"
    assert event["details"]["validated_loopback_port"] == 8000


def test_windows_launcher_and_interpreter_topology_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = SimpleNamespace(
        pid=19748,
        state="RUNNING",
        desired_state="RUNNING",
        process_create_time="2026-09-23T15:02:46.673Z",
        parent_pid=22152,
        process_tree=(3084, 19748),
        process_identities=(
            (3084, "2026-09-23T15:02:46.753Z"),
            (19748, "2026-09-23T15:02:46.673Z"),
        ),
    )
    service = _service(tmp_path, _StatusSupervisor(record))
    monkeypatch.setattr("services.control.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    try:
        assert asyncio.run(service._api_responsive()) is True
    finally:
        service.database.dispose()


def test_verified_api_without_listener_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, _StatusSupervisor(_verified_api()))

    def unavailable(*_args, **_kwargs):
        raise ConnectionRefusedError("no listener")

    monkeypatch.setattr("services.control.socket.create_connection", unavailable)
    try:
        assert asyncio.run(service._api_responsive()) is False
    finally:
        service.database.dispose()


@pytest.mark.parametrize(
    "record",
    [
        _verified_api(state="DEGRADED"),
        _verified_api(desired="STOPPED"),
        SimpleNamespace(pid=8000, state="RUNNING", desired_state="RUNNING", process_identities=()),
        SimpleNamespace(pid=8000, state="RUNNING", desired_state="RUNNING", process_identities=((7999, "reused"),)),
    ],
)
def test_unverified_or_stale_api_process_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record
) -> None:
    service = _service(tmp_path, _StatusSupervisor(record))
    monkeypatch.setattr("services.control.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    try:
        assert asyncio.run(service._api_responsive()) is False
    finally:
        service.database.dispose()


def test_non_loopback_api_configuration_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _StatusSupervisor(_verified_api())
    service = _service(tmp_path, supervisor)
    service.settings = replace(service.settings, api_host="0.0.0.0")
    monkeypatch.setattr("services.control.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    try:
        assert asyncio.run(service._api_responsive()) is False
    finally:
        service.database.dispose()


def test_start_does_not_rollback_verified_protected_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StartupSupervisor:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.stopped: list[str] = []

        def reconcile(self):
            return self.status()

        def status(self):
            if not self.started:
                return {
                    "api": _verified_api(state="STOPPED", desired="STOPPED"),
                    "live": _verified_api(pid=8001, state="STOPPED", desired="STOPPED"),
                }
            return {"api": _verified_api(), "live": _verified_api(pid=8001)}

        def start_component(self, component, _command):
            self.started.append(component)
            return _verified_api(pid=8000 if component == "api" else 8001)

        def last_start_spawned(self, _component):
            return True

        def stop_component(self, component):
            self.stopped.append(component)
            return SimpleNamespace(state="STOPPED")

    supervisor = StartupSupervisor()
    service = _service(tmp_path, supervisor)
    service.mt5_bootstrap = SimpleNamespace(
        ensure_ready=lambda _database: asyncio.sleep(
            0,
            result=SimpleNamespace(
                ready=True,
                launch_state="ALREADY_RUNNING",
                pid=None,
                checks={"terminal": "CONNECTED", "account": "VERIFIED", "market_data": "READY"},
                verification={},
            ),
        )
    )

    async def complete_verification():
        assert await service._api_responsive() is True
        return {
            "complete": True,
            "checks": {
                "api": "OK",
                "live_runtime": "CONNECTED",
                "mt5": "CONNECTED",
                "snapshot": "COMPLETE",
                "database": "CONNECTED",
                "forward": "CONNECTED",
            },
        }

    service._wait_for_startup_verification = complete_verification
    monkeypatch.setattr("services.control.migrate_database", lambda *_args: None)
    monkeypatch.setattr("services.control.resolve_supervised_python", lambda: "python.exe")
    monkeypatch.setattr("services.control.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    try:
        response = asyncio.run(service._start_infrastructure_locked())
    finally:
        service.database.dispose()

    assert response.startswith("🟢 ระบบพร้อมทำงาน")
    assert supervisor.started == ["api", "live"]
    assert supervisor.stopped == []
