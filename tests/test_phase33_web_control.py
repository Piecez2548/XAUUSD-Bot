from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
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
from services.demo_execution import demo_execution_armed


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
                "supervisor": "CONNECTED",
                "api": "RUNNING",
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


class FakeLifecycleSupervisor:
    def __init__(self, *, api_state: str = "RUNNING", live_state: str = "STOPPED",
                 live_start_state: str = "RUNNING") -> None:
        self.records = {
            "api": SimpleNamespace(state=api_state, desired_state="RUNNING"),
            "live": SimpleNamespace(state=live_state, desired_state="STOPPED"),
        }
        self.live_start_state = live_start_state
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.api_generations = 1 if api_state == "RUNNING" else 0
        self.live_generations = 0
        self._last_start_spawned: dict[str, bool] = {}

    def status(self):
        return self.records

    def reconcile(self):
        return self.status()

    def start_component(self, component, _command):
        self.started.append(component)
        record = self.records[component]
        was_running = record.state == "RUNNING"
        record.desired_state = "RUNNING"
        record.state = self.live_start_state if component == "live" else "RUNNING"
        self._last_start_spawned[component] = component == "live"
        if component == "api" and not was_running:
            self.api_generations += 1
        if component == "live" and record.state == "RUNNING":
            self.live_generations += 1
        return record

    def last_start_spawned(self, component):
        return self._last_start_spawned.get(component, False)

    def stop_component(self, component, **_kwargs):
        self.stopped.append(component)
        record = self.records[component]
        record.state = "STOPPED"
        record.desired_state = "STOPPED"
        return record


class FakeBootstrap:
    async def ensure_ready(self, _database):
        return SimpleNamespace(
            ready=True,
            launch_state="REUSED",
            pid=1,
            checks={"terminal": "CONNECTED", "account": "VERIFIED", "market_data": "READY"},
            verification={},
        )


def _lifecycle_service(
    tmp_path: Path,
    database: Database,
    supervisor: FakeLifecycleSupervisor,
) -> TelegramControlService:
    service = TelegramControlService(
        Settings(demo_execution_enabled=True, forward_shadow_enabled=False),
        tmp_path,
        database=database,
        supervisor=supervisor,
        mt5_bootstrap=FakeBootstrap(),
    )
    return service


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


def test_control_plane_bootstraps_api_without_starting_live(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = FakeLifecycleSupervisor(api_state="STOPPED", live_state="STOPPED")
    service = _lifecycle_service(tmp_path, control_database, supervisor)
    monkeypatch.setattr(service, "_api_responsive", lambda: asyncio.sleep(0, result=True))

    assert asyncio.run(service._ensure_control_plane_api()) is True
    assert supervisor.started == ["api"]
    assert supervisor.records["api"].state == "RUNNING"
    assert supervisor.records["live"].state == "STOPPED"


def test_web_start_stop_restart_preserve_one_api_control_plane(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = FakeLifecycleSupervisor()
    service = _lifecycle_service(tmp_path, control_database, supervisor)
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)
    service._monitoring_is_healthy = lambda: asyncio.sleep(0, result=False)
    service._wait_for_startup_verification = lambda: asyncio.sleep(0, result={
        "complete": True,
        "checks": {
            "api": "OK",
            "live_runtime": "CONNECTED",
            "mt5": "CONNECTED",
            "snapshot": "COMPLETE",
            "database": "CONNECTED",
            "forward": "DISABLED",
        },
    })
    service._system_ready_response = lambda *_args, **_kwargs: "READY"

    operation_id = str(uuid4())
    started = asyncio.run(
        service._handle_ipc_request({"command": "start", "operation_id": operation_id})
    )
    replay = asyncio.run(
        service._handle_ipc_request({"command": "start", "operation_id": operation_id})
    )
    assert started["ok"] is True
    assert replay["duplicate"] is True
    assert supervisor.live_generations == 1
    assert supervisor.records["api"].state == "RUNNING"
    assert supervisor.records["live"].state == "RUNNING"

    stopped = asyncio.run(
        service._handle_ipc_request({"command": "stop", "operation_id": str(uuid4())})
    )
    assert stopped["ok"] is True
    assert supervisor.records["live"].state == "STOPPED"
    assert supervisor.records["api"].state == "RUNNING"
    assert supervisor.stopped == ["live"]
    assert "Control Plane" in stopped["message"]
    assert stopped["status"]["supervisor"] == "CONNECTED"

    demo_on = asyncio.run(
        service._handle_ipc_request({"command": "demo_on", "operation_id": str(uuid4())})
    )
    assert demo_on["ok"] is True
    assert demo_execution_armed(control_database) is True
    demo_off = asyncio.run(
        service._handle_ipc_request({"command": "demo_off", "operation_id": str(uuid4())})
    )
    assert demo_off["ok"] is True
    assert demo_execution_armed(control_database) is False

    restarted = asyncio.run(
        service._handle_ipc_request({"command": "restart", "operation_id": str(uuid4())})
    )
    assert restarted["ok"] is True
    assert supervisor.live_generations == 2
    assert supervisor.api_generations == 1
    assert supervisor.records["api"].state == "RUNNING"
    assert supervisor.records["live"].state == "RUNNING"
    assert supervisor.stopped == ["live", "live"]


def test_failed_live_start_rolls_back_live_only(
    control_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = FakeLifecycleSupervisor(live_start_state="ERROR")
    service = _lifecycle_service(tmp_path, control_database, supervisor)
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)
    service._monitoring_is_healthy = lambda: asyncio.sleep(0, result=False)

    response = asyncio.run(service._start_infrastructure_locked())

    assert response.startswith("🟠 START INCOMPLETE")
    assert supervisor.stopped == ["live"]
    assert supervisor.records["api"].state == "RUNNING"
    assert supervisor.records["live"].state == "STOPPED"


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
