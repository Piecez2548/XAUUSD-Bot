from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import services.control as control_module
from api.app import create_app
from config.remote_read_only_policy import is_private_control_path_allowed
from config.settings import Settings
from persistence.database import Database
from persistence.orm import ControlAuditRecord
from services.control import TelegramControlService
from services.control_ipc import ControlIpcError, validate_ipc_request


class FakeControlIpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def request(self, command: str, operation_id: str, *, actor: str) -> dict[str, object]:
        self.calls.append((command, operation_id, actor))
        return {
            "ok": True,
            "duplicate": False,
            "operation_id": operation_id,
            "command": command,
            "message": "accepted",
            "status": {
                "checked_at": "2026-09-24T00:00:00+00:00",
                "control": "CONNECTED",
                "supervisor": "STOPPED",
                "api": "STOPPED",
                "live": "STOPPED",
                "mt5": "CONNECTED",
                "database": "CONNECTED",
                "telegram": "CONNECTED",
                "forward_shadow": "DISABLED",
                "execution": {
                    "demo_execution_enabled": False,
                    "demo_kill_switch_armed": False,
                    "real_money_execution": "DISABLED",
                },
                "strategy": {
                    "pair_zone_state": "UNKNOWN",
                    "current_direction": "UNKNOWN",
                    "latest_canonical_signal_id": None,
                    "latest_canonical_direction": None,
                    "latest_canonical_signal_at": None,
                    "latest_forward_session_id": None,
                },
            },
        }


@pytest.fixture
def control_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'web-control.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


def test_control_ipc_protocol_is_fixed_and_fail_closed() -> None:
    operation_id = str(uuid4())
    assert (
        validate_ipc_request({"command": "status", "operation_id": operation_id})["command"]
        == "status"
    )
    with pytest.raises(ControlIpcError):
        validate_ipc_request({"command": "shell", "operation_id": operation_id})
    with pytest.raises(ControlIpcError):
        validate_ipc_request({"command": "start", "operation_id": operation_id, "extra": "x"})
    with pytest.raises(ControlIpcError):
        validate_ipc_request({"command": "start", "operation_id": "not-a-uuid"})


@pytest.mark.parametrize(
    ("path", "method", "allowed"),
    [
        ("/api/control/status", "GET", True),
        ("/api/control/demo-status", "GET", True),
        ("/api/control/start", "POST", True),
        ("/api/control/stop", "POST", True),
        ("/api/control/restart", "POST", True),
        ("/api/control/demo-on", "POST", True),
        ("/api/control/demo-off", "POST", True),
        ("/api/control/start", "GET", False),
        ("/api/control/status", "POST", False),
        ("/api/control/unknown", "POST", False),
        ("/api/control/../../start", "POST", False),
    ],
)
def test_private_control_route_allowlist(path: str, method: str, allowed: bool) -> None:
    assert is_private_control_path_allowed(path, method) is allowed


def test_authenticated_control_endpoints_and_unauthenticated_denial(
    control_database: Database,
) -> None:
    fake = FakeControlIpc()
    app = create_app(
        settings=Settings(remote_dashboard_mode=True),
        database=control_database,
        control_ipc=fake,
    )
    with TestClient(app) as client:
        assert client.get("/api/control/status").status_code == 401
        assert (
            client.post("/api/control/start", json={"operation_id": str(uuid4())}).status_code
            == 401
        )
        headers = {"Tailscale-User-Login": "operator@example.com"}
        status = client.get("/api/control/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["status"]["execution"]["real_money_execution"] == "DISABLED"
        assert client.get("/api/control/start", headers=headers).status_code == 404
        assert (
            client.post(
                "/api/control/start",
                headers=headers,
                json={"operation_id": str(uuid4())},
            ).status_code
            == 200
        )
        assert client.post(
            "/api/control/start",
            headers=headers,
            json={"operation_id": str(uuid4()), "command": "shell"},
        ).status_code == 422
        assert client.post(
            "/api/control/unknown",
            headers=headers,
            json={"operation_id": str(uuid4())},
        ).status_code == 404
    assert fake.calls and fake.calls[-1][0] == "start"


def test_control_plane_is_disabled_when_private_dashboard_mode_is_off(
    control_database: Database,
) -> None:
    app = create_app(
        settings=Settings(remote_dashboard_mode=False),
        database=control_database,
        control_ipc=FakeControlIpc(),
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/control/status",
            headers={"Tailscale-User-Login": "spoofed@example.com"},
        )
    assert response.status_code == 404


def test_control_ipc_replay_does_not_execute_demo_operation_twice(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(demo_execution_enabled=True)
    service = TelegramControlService(
        settings,
        tmp_path,
        database=control_database,
        logger=logging.getLogger("phase33-control"),
    )
    calls: list[bool] = []

    def record(*args, **kwargs):
        calls.append(bool(args[1]))

    monkeypatch.setattr(control_module, "set_demo_execution_enabled", record)
    operation_id = str(uuid4())
    first = asyncio.run(
        service._handle_ipc_request({"command": "demo_off", "operation_id": operation_id})
    )
    second = asyncio.run(
        service._handle_ipc_request({"command": "demo_off", "operation_id": operation_id})
    )
    assert first["ok"] is True
    assert second["duplicate"] is True
    assert calls == [False]
    with control_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ControlAuditRecord)) == 1


def test_web_lifecycle_commands_use_the_same_locked_handlers(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TelegramControlService(Settings(), tmp_path, database=control_database)
    calls: list[str] = []

    async def fake_start() -> str:
        calls.append("start")
        return "STARTED"

    async def fake_stop() -> str:
        calls.append("stop")
        return "STOPPED"

    async def fake_restart() -> str:
        calls.append("restart")
        return "RESTARTED"

    monkeypatch.setattr(service, "_start_infrastructure_locked", fake_start)
    monkeypatch.setattr(service, "_stop_infrastructure_locked", fake_stop)
    monkeypatch.setattr(service, "_restart_infrastructure_locked", fake_restart)
    for command, expected in (("start", "STARTED"), ("stop", "STOPPED"), ("restart", "RESTARTED")):
        response = asyncio.run(
            service._handle_ipc_request({"command": command, "operation_id": str(uuid4())})
        )
        assert response["message"] == expected
    assert calls == ["start", "stop", "restart"]


def test_web_get_status_does_not_write_control_audit(
    control_database: Database, tmp_path: Path
) -> None:
    service = TelegramControlService(Settings(), tmp_path, database=control_database)
    response = asyncio.run(
        service._handle_ipc_request({"command": "status", "operation_id": str(uuid4())})
    )
    assert response["ok"] is True
    with control_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ControlAuditRecord)) == 0


def test_web_lifecycle_requests_are_serialized(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TelegramControlService(Settings(), tmp_path, database=control_database)
    active = 0
    maximum = 0

    async def fake_start() -> str:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "STARTED"

    monkeypatch.setattr(service, "_start_infrastructure_locked", fake_start)

    async def run_pair() -> None:
        await asyncio.gather(
            service._handle_ipc_request({"command": "start", "operation_id": str(uuid4())}),
            service._handle_ipc_request({"command": "start", "operation_id": str(uuid4())}),
        )

    asyncio.run(run_pair())
    assert maximum == 1


def test_background_task_installer_preserves_single_hidden_control_and_battery_safety() -> None:
    script = Path("scripts/install_background_control_task.ps1").read_text(encoding="utf-8")
    assert '"XAUUSD Bot Background"' in script
    assert "-Hidden" in script
    assert "-StartWhenAvailable" in script
    assert "-DontStopIfGoingOnBatteries" in script
    assert "-Argument" in script and "control" in script


def test_demo_enable_is_disabled_by_configuration_and_never_real_money(
    control_database: Database, tmp_path: Path
) -> None:
    service = TelegramControlService(
        Settings(demo_execution_enabled=False),
        tmp_path,
        database=control_database,
    )
    response = asyncio.run(
        service._handle_ipc_request({"command": "demo_on", "operation_id": str(uuid4())})
    )
    assert response["ok"] is True
    assert "DISABLED" in str(response["message"])
    assert response["status"]["execution"]["real_money_execution"] == "DISABLED"
