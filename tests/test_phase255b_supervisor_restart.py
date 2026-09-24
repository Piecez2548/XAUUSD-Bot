from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.app as api_module
import mt5.bootstrap as mt5_bootstrap_module
import services.control as control_module
import services.supervisor as supervisor_module
from api.app import _supervisor_health_state
from config.settings import Settings
from persistence.database import Database
from persistence.orm import SystemHealthRecord
from services.control import TelegramControlService
from services.supervisor import ProcessIdentity, ProcessRecord, ProcessSupervisor

COMMAND = [r"C:\bot\.venv\Scripts\python.exe", r"D:\bot\main.py", "live"]


def _identity(pid: int, parent_pid: int | None, creation_time: str, line: str) -> ProcessIdentity:
    return ProcessIdentity(pid, parent_pid, creation_time, r"C:\Python311\python.exe", line)


def _snapshot(*identities: ProcessIdentity) -> dict[int, ProcessIdentity]:
    return {item.pid: item for item in identities}


def _line(executable: str = r"C:\other\python.exe") -> str:
    return subprocess.list2cmdline([executable, *COMMAND[1:]])


def _record(*, pid: int = 10, creation_time: str | None = "old") -> ProcessRecord:
    return ProcessRecord(
        component="live",
        pid=pid,
        state="RUNNING",
        started_at="2026-09-23T00:00:00+00:00",
        last_heartbeat="2026-09-23T00:00:00+00:00",
        exit_code=None,
        desired_state="RUNNING",
        command=tuple(COMMAND),
        process_create_time=creation_time,
        process_tree=(pid,),
        process_identities=((pid, creation_time),),
    )


def test_command_matching_requires_exact_script_path_and_arguments() -> None:
    exact = _line()
    assert supervisor_module._command_matches(exact, COMMAND)
    assert not supervisor_module._command_matches(
        subprocess.list2cmdline([r"C:\other\python.exe", r"D:\other\main.py", "live"]), COMMAND
    )
    assert not supervisor_module._command_matches(
        subprocess.list2cmdline([r"C:\other\python.exe", r"D:\bot\main.py", "livestream"]), COMMAND
    )
    assert not supervisor_module._command_matches(
        subprocess.list2cmdline([r"C:\other\python.exe", "main.py", "live"]), COMMAND
    )


@pytest.mark.parametrize("platform_name, expect_no_window", [("nt", True), ("posix", False)])
def test_supervised_popen_uses_windows_no_console_flag_only_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expect_no_window: bool,
) -> None:
    class FakeChild:
        pid = 321

        def poll(self) -> None:
            return None

    captured: dict[str, object] = {}
    child = FakeChild()
    monkeypatch.setattr(
        supervisor_module, "_is_windows", lambda: platform_name == "nt"
    )
    monkeypatch.setattr(supervisor_module, "_find_matching_processes", lambda _command: [])
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(
        supervisor_module.ProcessSupervisor, "_wait_for_identity", lambda *_args: None
    )
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda *_args, **kwargs: captured.update(kwargs) or child,
    )

    supervisor = ProcessSupervisor(tmp_path)
    supervisor.start_component("live", COMMAND)

    flags = int(captured["creationflags"])
    no_window = getattr(supervisor_module.subprocess, "CREATE_NO_WINDOW", 0)
    assert bool(flags & no_window) is expect_no_window
    assert captured["shell"] is False
    assert captured["stderr"] is subprocess.STDOUT
    assert captured["stdout"] is not subprocess.DEVNULL
    supervisor._close_diagnostics("live")


def test_pythonw_resolves_to_verified_sibling_console_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    windowless = scripts / "pythonw.exe"
    console = scripts / "python.exe"
    windowless.write_bytes(b"")
    console.write_bytes(b"")
    monkeypatch.setattr(supervisor_module, "_is_windows", lambda: True)
    monkeypatch.setattr(supervisor_module.sys, "executable", str(windowless))

    assert supervisor_module.resolve_supervised_python() == str(console)


def test_missing_console_interpreter_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windowless = tmp_path / "Scripts" / "pythonw.exe"
    windowless.parent.mkdir()
    windowless.write_bytes(b"")
    monkeypatch.setattr(supervisor_module, "_is_windows", lambda: True)
    monkeypatch.setattr(supervisor_module.sys, "executable", str(windowless))

    with pytest.raises(RuntimeError, match="console Python interpreter is unavailable"):
        supervisor_module.resolve_supervised_python()


def test_non_windows_interpreter_resolution_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "_is_windows", lambda: False)
    monkeypatch.setattr(supervisor_module.sys, "executable", "custom-python")

    assert supervisor_module.resolve_supervised_python() == "custom-python"


def test_control_commands_use_resolved_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = object.__new__(TelegramControlService)
    service.project_root = tmp_path
    console_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(control_module, "resolve_supervised_python", lambda: console_python)

    commands = service._supervised_commands()

    assert commands["api"][0] == console_python
    assert commands["live"][0] == console_python


def test_reconcile_clears_conclusively_absent_persisted_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"live": asdict(_record(pid=777))}), encoding="utf-8")
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)

    records = ProcessSupervisor(tmp_path, registry_path=registry).reconcile()
    persisted = json.loads(registry.read_text(encoding="utf-8"))

    assert records["live"].state == "STOPPED"
    assert records["live"].pid is None
    assert records["live"].desired_state == "RUNNING"
    assert persisted["live"]["state"] == "STOPPED"
    assert persisted["live"]["pid"] is None


def test_exact_stale_windows_registry_with_two_absent_pids_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(pid=26600)
    record.parent_pid = 26268
    record.process_tree = (18480, 26600)
    record.process_identities = (
        (18480, "/Date(1790143228542)/"),
        (26600, "/Date(1790143228523)/"),
    )
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"live": asdict(record)}), encoding="utf-8")
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)

    result = ProcessSupervisor(tmp_path, registry_path=registry).reconcile()["live"]

    assert result.state == "STOPPED"
    assert result.pid is None
    assert result.parent_pid is None
    assert result.process_tree == ()
    assert result.process_identities == ()
    saved = json.loads(registry.read_text(encoding="utf-8"))["live"]
    assert saved["state"] == "STOPPED"
    assert saved["pid"] is None
    assert saved["process_tree"] == []
    assert saved["process_identities"] == []


def test_windows_creation_time_serialization_is_preserved_as_opaque_identity(
    tmp_path: Path,
) -> None:
    record = _record(pid=26600)
    record.process_create_time = "/Date(1790143228523)/"
    record.process_tree = (18480, 26600)
    record.process_identities = (
        (18480, "/Date(1790143228542)/"),
        (26600, "/Date(1790143228523)/"),
    )
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"live": asdict(record)}), encoding="utf-8")

    loaded = ProcessSupervisor(tmp_path, registry_path=registry)._load()["live"]

    assert loaded.process_create_time == "/Date(1790143228523)/"
    assert loaded.process_identities == (
        (18480, "/Date(1790143228542)/"),
        (26600, "/Date(1790143228523)/"),
    )


def test_windows_os_kill_systemerror_isolated_probe_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_windows_access_error(_pid, _signal):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    monkeypatch.setattr(supervisor_module.os, "kill", raise_windows_access_error)

    assert supervisor_module._pid_alive(26600) is False


def test_systemerror_reconciliation_writes_redacted_local_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingReconcileSupervisor:
        def reconcile(self):
            raise SystemError("Access denied token=super-secret-token")

        def status(self):
            return {
                "api": SimpleNamespace(state="STOPPED"),
                "live": SimpleNamespace(state="STOPPED"),
            }

        def start_component(self, _component, _command):
            raise AssertionError("supervised children must not spawn after reconciliation failure")

    async def fail_if_mt5_called(_database):
        raise AssertionError("MT5 must not be reached after reconciliation failure")

    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    service = TelegramControlService(
        Settings(telegram_bot_token="super-secret-token"),
        tmp_path,
        database=database,
        supervisor=FailingReconcileSupervisor(),
        mt5_bootstrap=SimpleNamespace(ensure_ready=fail_if_mt5_called),
    )

    try:
        response = asyncio.run(service._start_infrastructure_locked())
        diagnostic = tmp_path / "logs" / "supervisor_control_diagnostics.jsonl"
        payload = json.loads(diagnostic.read_text(encoding="utf-8").splitlines()[-1])
    finally:
        database.dispose()

    assert response.startswith("🟠 START INCOMPLETE")
    assert "SystemError" in response
    assert "super-secret-token" not in response
    assert payload["operation"] == "/start"
    assert payload["stage"] == "supervisor_reconciliation"
    assert payload["exception_type"] == "SystemError"
    assert "super-secret-token" not in json.dumps(payload)
    assert "traceback" in payload


def test_clean_os_and_stale_pythonw_record_spawn_with_new_python_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_command = [r"C:\bot\.venv\Scripts\pythonw.exe", r"D:\bot\main.py", "live"]
    new_command = [r"C:\bot\.venv\Scripts\python.exe", r"D:\bot\main.py", "live"]
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "live": asdict(ProcessRecord(
            component="live", pid=None, state="STOPPED", started_at=None,
            last_heartbeat=None, exit_code=0, desired_state="STOPPED",
            command=tuple(old_command),
        ))
    }), encoding="utf-8")

    class FakeChild:
        pid = 321

        def poll(self):
            return None

    captured: dict[str, object] = {}
    monkeypatch.setattr(supervisor_module, "_find_matching_processes", lambda _command: [])
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(
        supervisor_module.ProcessSupervisor, "_wait_for_identity", lambda *_args: None
    )
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda command, **kwargs: captured.update(command=command, **kwargs) or FakeChild(),
    )

    supervisor = ProcessSupervisor(tmp_path, registry_path=registry)
    result = supervisor.start_component("live", new_command)

    assert captured["command"] == new_command
    assert result.desired_state == "RUNNING"
    assert supervisor.last_start_spawned("live") is True
    supervisor._close_diagnostics("live")


def test_matching_but_unverified_topology_fails_closed_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"live": asdict(_record(pid=10))}), encoding="utf-8")
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor_module, "_find_matching_processes", lambda _command: [10])
    monkeypatch.setattr(supervisor_module, "_record_matches_process", lambda _record: False)
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("unverified matching topology must not spawn"),
    )

    result = ProcessSupervisor(tmp_path, registry_path=registry).start_component("live", COMMAND)

    assert result.state == "DEGRADED"
    assert "identity" in (result.last_error or "")


def test_stale_reconciliation_never_terminates_unrelated_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"live": asdict(_record(pid=10))}), encoding="utf-8")
    unrelated = _identity(10, 1, "new", subprocess.list2cmdline([
        r"C:\other\python.exe", r"D:\other\main.py", "worker"
    ]))
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: _snapshot(unrelated))
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        supervisor_module,
        "_terminate_owned_processes",
        lambda *_args: pytest.fail("reconciliation must never terminate unrelated Python"),
    )

    result = ProcessSupervisor(tmp_path, registry_path=registry).reconcile()["live"]

    assert result.state == "DEGRADED"
    assert result.pid == 10


def test_windows_cleanup_does_not_probe_os_kill_before_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def invalid_windows_probe(_pid: int) -> bool:
        raise OSError(22, "The parameter is incorrect", None, 87)

    def fake_taskkill(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=128, stdout="", stderr="process not found")

    monkeypatch.setattr(supervisor_module.os, "name", "nt")
    monkeypatch.setattr(supervisor_module, "_pid_alive", invalid_windows_probe)
    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_taskkill)

    supervisor_module._terminate_owned_processes([321], None)

    assert len(calls) == 1
    assert calls[0][0] == ["taskkill.exe", "/PID", "321", "/F"]
    assert calls[0][1]["check"] is False


def test_start_path_reconciles_before_pre_popen_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"live": asdict(_record(pid=777))}), encoding="utf-8")
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)

    async def fail_mt5(_database):
        raise RuntimeError("test MT5 bootstrap failure")

    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    service = TelegramControlService(
        Settings(),
        tmp_path,
        database=database,
        supervisor=ProcessSupervisor(tmp_path, registry_path=registry),
        mt5_bootstrap=SimpleNamespace(ensure_ready=fail_mt5),
    )

    try:
        response = asyncio.run(service._start_infrastructure_locked())
        persisted = json.loads(registry.read_text(encoding="utf-8"))
    finally:
        database.dispose()

    assert response.startswith("🟠 START INCOMPLETE")
    assert "MT5 readiness failed (RuntimeError)" in response
    assert "API=NOT_STARTED" in response and "LIVE=NOT_STARTED" in response
    assert persisted["live"]["state"] == "STOPPED"
    assert persisted["live"]["pid"] is None


def test_start_uses_resolved_python_for_both_supervised_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CapturingSupervisor:
        def __init__(self) -> None:
            self.commands: list[tuple[str, list[str]]] = []

        def reconcile(self):
            return self.status()

        def status(self):
            return {
                "api": SimpleNamespace(state="STOPPED"),
                "live": SimpleNamespace(state="STOPPED"),
            }

        def start_component(self, component, command):
            self.commands.append((component, command))
            return SimpleNamespace(component=component, state="RUNNING", last_error=None)

        def last_start_spawned(self, _component):
            return False

        def stop_component(self, _component):
            return SimpleNamespace(state="STOPPED")

    async def ready_mt5(_database):
        return SimpleNamespace(ready=True)

    async def incomplete_verification():
        return {"complete": False, "checks": {"api": "OK", "live": "WAITING"}}

    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    supervisor = CapturingSupervisor()
    service = TelegramControlService(
        Settings(),
        tmp_path,
        database=database,
        supervisor=supervisor,
        mt5_bootstrap=SimpleNamespace(ensure_ready=ready_mt5),
    )
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)
    monkeypatch.setattr(
        control_module,
        "resolve_supervised_python",
        lambda: r"C:\bot\.venv\Scripts\python.exe",
    )
    monkeypatch.setattr(service, "_wait_for_startup_verification", incomplete_verification)

    try:
        asyncio.run(service._start_infrastructure_locked())
    finally:
        database.dispose()

    assert [component for component, _command in supervisor.commands] == ["api", "live"]
    assert {command[0] for _component, command in supervisor.commands} == {
        r"C:\bot\.venv\Scripts\python.exe"
    }


def test_launcher_interpreter_topology_stabilizes_before_running_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeChild:
        pid = 100

        def poll(self):
            return None

    line = _line(r"C:\bot\.venv\Scripts\python.exe")
    topology = _snapshot(
        _identity(100, 1, "root", line),
        _identity(101, 100, "child", line),
    )
    snapshots = iter([{}, topology, topology])
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(supervisor_module, "_find_matching_processes", lambda _command: [])
    monkeypatch.setattr(
        supervisor_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeChild()
    )

    result = ProcessSupervisor(tmp_path).start_component("live", COMMAND)

    assert result.state == "RUNNING"
    assert result.pid == 100
    assert result.process_tree == (100, 101)


def test_stable_topology_never_relaxes_ownership_after_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeChild:
        pid = 100

        def poll(self):
            return None

    monkeypatch.setattr(supervisor_module.ProcessSupervisor, "IDENTITY_STABILIZATION_SECONDS", 0)
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(supervisor_module, "_find_matching_processes", lambda _command: [])
    monkeypatch.setattr(
        supervisor_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeChild()
    )

    result = ProcessSupervisor(tmp_path).start_component("live", COMMAND)

    assert result.state == "DEGRADED"
    assert "identity" in (result.last_error or "")


def test_successful_api_then_failed_verification_persists_rollback_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Supervisor:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.stopped: list[str] = []

        def reconcile(self):
            return self.status()

        def status(self):
            return {
                "api": SimpleNamespace(state="STOPPED"),
                "live": SimpleNamespace(state="STOPPED"),
            }

        def start_component(self, component, _command):
            self.started.append(component)
            return SimpleNamespace(
                component=component,
                pid=100 if component == "api" else 101,
                parent_pid=1,
                process_tree=(100,) if component == "api" else (101,),
                process_identities=(
                    ((100, "api-root"),)
                    if component == "api"
                    else ((101, "live-root"),)
                ),
                state="RUNNING",
                last_error=None,
            )

        def last_start_spawned(self, _component):
            return True

        def stop_component(self, component):
            self.stopped.append(component)
            return SimpleNamespace(state="STOPPED")

    async def ready_mt5(_database):
        return SimpleNamespace(ready=True)

    async def failed_verification():
        return {"complete": False, "checks": {"api": "OK", "live_runtime": "UNKNOWN"}}

    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    supervisor = Supervisor()
    service = TelegramControlService(
        Settings(),
        tmp_path,
        database=database,
        supervisor=supervisor,
        mt5_bootstrap=SimpleNamespace(ensure_ready=ready_mt5),
    )
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)
    monkeypatch.setattr(service, "_wait_for_startup_verification", failed_verification)

    try:
        response = asyncio.run(service._start_infrastructure_locked())
        diagnostic = tmp_path / "logs" / "supervisor_control_diagnostics.jsonl"
        events = [json.loads(line) for line in diagnostic.read_text(encoding="utf-8").splitlines()]
    finally:
        database.dispose()

    assert response.startswith("🟠 START INCOMPLETE")
    assert supervisor.started == ["api", "live"]
    assert supervisor.stopped == ["live"]
    rollback = [event for event in events if event.get("stage") == "startup_rollback"]
    assert {event["component"] for event in rollback} == {"live"}
    assert all(event["details"]["final_state"] == "STOPPED" for event in rollback)
    assert all(
        "startup verification incomplete" in event["details"]["rollback_trigger"]
        for event in rollback
    )


def test_windows_process_inspection_helpers_use_no_console_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def fake_run(*_args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(stdout="[]")

    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_run)
    supervisor_module._process_snapshot()

    mt5_captured: list[dict[str, object]] = []

    def fake_mt5_run(*_args, **kwargs):
        mt5_captured.append(kwargs)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(mt5_bootstrap_module.subprocess, "run", fake_mt5_run)
    mt5_bootstrap_module._default_process_probe()

    no_window = getattr(supervisor_module.subprocess, "CREATE_NO_WINDOW", 0)
    assert captured and int(captured[0]["creationflags"]) & no_window
    assert mt5_captured and int(mt5_captured[0]["creationflags"]) & no_window


def test_failed_api_start_fails_closed_without_starting_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailedApiSupervisor:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.stopped: list[str] = []

        def status(self):
            return {
                "api": SimpleNamespace(state="STOPPED"),
                "live": SimpleNamespace(state="STOPPED"),
            }

        def start_component(self, component, _command):
            self.started.append(component)
            return SimpleNamespace(
                component=component,
                state="ERROR" if component == "api" else "RUNNING",
                last_error="supervised process exited with code 1; see logs/supervisor_api.log",
            )

        def last_start_spawned(self, _component):
            return False

        def stop_component(self, component):
            self.stopped.append(component)
            return SimpleNamespace(state="STOPPED")

    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    supervisor = FailedApiSupervisor()
    service = TelegramControlService(
        Settings(),
        tmp_path,
        database=database,
        supervisor=supervisor,
        mt5_bootstrap=SimpleNamespace(
            ensure_ready=lambda _database: asyncio.sleep(
                0, result=SimpleNamespace(ready=True)
            )
        ),
    )
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)

    try:
        response = asyncio.run(service._start_infrastructure_locked())
    finally:
        database.dispose()

    assert response.startswith("🟠 START INCOMPLETE")
    assert "API=ERROR" in response
    assert supervisor.started == ["api"]
    assert supervisor.stopped == []


def test_startup_rollback_does_not_stop_preexisting_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PartiallyRunningSupervisor:
        def __init__(self) -> None:
            self.stopped: list[str] = []

        def status(self):
            return {
                "api": SimpleNamespace(state="RUNNING"),
                "live": SimpleNamespace(state="STOPPED"),
            }

        def start_component(self, component, _command):
            return SimpleNamespace(
                component=component,
                state="RUNNING" if component == "api" else "ERROR",
                last_error="live startup failed",
            )

        def last_start_spawned(self, component):
            return component == "live"

        def stop_component(self, component):
            self.stopped.append(component)
            return SimpleNamespace(state="STOPPED")

    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    supervisor = PartiallyRunningSupervisor()
    service = TelegramControlService(
        Settings(),
        tmp_path,
        database=database,
        supervisor=supervisor,
        mt5_bootstrap=SimpleNamespace(
            ensure_ready=lambda _database: asyncio.sleep(
                0, result=SimpleNamespace(ready=True)
            )
        ),
    )
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)

    try:
        response = asyncio.run(service._start_infrastructure_locked())
    finally:
        database.dispose()

    assert response.startswith("🟠 START INCOMPLETE")
    assert supervisor.stopped == ["live"]


def test_immediate_startup_exit_is_reported_with_safe_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor_with_child(tmp_path, _ExitedChild(17), _record())
    monkeypatch.setattr(supervisor_module, "_record_matches_process", lambda _record: False)

    status = supervisor.status()["live"]

    assert status.state == "CRASHED"
    assert status.exit_code == 17
    assert "code 17" in (status.last_error or "")
    assert "supervisor_live.log" in (status.last_error or "")


def test_production_subprocess_launches_are_non_shell_and_scoped() -> None:
    supervisor_source = Path("services/supervisor.py").read_text(encoding="utf-8")
    mt5_source = Path("mt5/bootstrap.py").read_text(encoding="utf-8")

    assert "shell=True" not in supervisor_source
    assert "shell=True" not in mt5_source
    assert "CREATE_NO_WINDOW" in supervisor_source
    assert supervisor_source.count("creationflags=_subprocess_creation_flags()") >= 2
    assert "_background_creation_flags()" in mt5_source


def test_pid_reuse_and_missing_creation_time_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    line = _line()
    current = _identity(10, None, "new", line)
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: _snapshot(current))

    assert not supervisor_module._record_matches_process(_record(creation_time="old"))
    assert supervisor_module._verified_owned_pids(_record(creation_time="old"), None) is None
    assert not supervisor_module._record_matches_process(_record(creation_time=None))


def test_multiple_independent_matching_trees_degrade_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = _line()
    snapshot = _snapshot(
        _identity(10, 1, "a", line),
        _identity(20, 2, "b", line),
    )
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: snapshot)
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("ambiguous trees must not spawn a replacement"),
    )

    result = ProcessSupervisor(tmp_path).start_component("live", COMMAND)

    assert result.state == "DEGRADED"
    assert "multiple independent" in (result.last_error or "")


def test_unverified_spawn_is_degraded_and_marked_for_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeChild:
        pid = 321

        def poll(self) -> None:
            return None

    child = FakeChild()
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(
        supervisor_module.ProcessSupervisor, "_wait_for_identity", lambda *_args: None
    )
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", lambda *_args, **_kwargs: child)

    supervisor = ProcessSupervisor(tmp_path)
    result = supervisor.start_component("live", COMMAND)

    assert result.state == "DEGRADED"
    assert "identity" in (result.last_error or "")
    assert supervisor.last_start_spawned("live") is True
    assert supervisor.status()["live"].state == "DEGRADED"


def test_launcher_and_interpreter_are_one_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    line = '"C:\\other\\python.exe" "D:\\bot\\main.py" live'
    snapshot = _snapshot(
        _identity(10, 1, "root", line),
        _identity(11, 10, "child", line),
    )
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: snapshot)

    assert supervisor_module._find_matching_processes(COMMAND) == [10]
    assert supervisor_module._matching_descendants(10, snapshot, COMMAND) == {10, 11}
    record = _record(creation_time="root")
    record.process_tree = (10, 11)
    record.process_identities = ((10, "root"), (11, "child"))
    assert supervisor_module._verified_owned_pids(record, None) == [11, 10]


def test_verified_stop_only_uses_creation_time_verified_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = _line()
    snapshot = _snapshot(
        _identity(10, 1, "root", line),
        _identity(11, 10, "child", line),
    )
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: snapshot)
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: True)
    terminated: list[int] = []
    monkeypatch.setattr(
        supervisor_module,
        "_terminate_owned_processes",
        lambda pids, _child: terminated.extend(pids),
    )
    monkeypatch.setattr(supervisor_module, "_wait_for_pids_to_exit", lambda _pids, timeout: True)

    registry = tmp_path / "registry.json"
    payload = asdict(_record(creation_time="root"))
    payload["process_tree"] = [10, 11]
    payload["process_identities"] = [[10, "root"], [11, "child"]]
    registry.write_text(json.dumps({"live": payload}), encoding="utf-8")
    result = ProcessSupervisor(tmp_path, registry_path=registry).stop_component("live")

    assert result.state == "STOPPED"
    assert terminated == [11, 10]


def test_unverified_stop_does_not_terminate_pid_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = _line()
    current = _identity(10, None, "new", line)
    monkeypatch.setattr(supervisor_module, "_process_snapshot", lambda: _snapshot(current))
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        supervisor_module,
        "_terminate_owned_processes",
        lambda *_args: pytest.fail("PID-reused process must not be terminated"),
    )
    registry = tmp_path / "registry.json"
    payload = {
        "live": {
            "component": "live", "pid": 10, "state": "RUNNING",
            "started_at": "2026-09-23T00:00:00+00:00",
            "last_heartbeat": "2026-09-23T00:00:00+00:00", "exit_code": None,
            "command": COMMAND, "process_create_time": "old", "process_tree": [10],
            "process_identities": [[10, "old"]],
        }
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

    result = ProcessSupervisor(tmp_path, registry_path=registry).stop_component("live")

    assert result.state == "DEGRADED"
    assert "unverified" in (result.last_error or "")


def test_restart_does_not_start_after_incomplete_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = ProcessSupervisor(tmp_path)
    stopped = _record()
    stopped.state = "DEGRADED"
    monkeypatch.setattr(supervisor, "stop_component", lambda _component: stopped)
    monkeypatch.setattr(
        supervisor,
        "start_component",
        lambda *_args: pytest.fail("replacement must not start after incomplete stop"),
    )

    result = supervisor.restart_component("live", COMMAND)

    assert result.state == "DEGRADED"


class _ExitedChild:
    pid = 10

    def __init__(self, code: int | None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


def _supervisor_with_child(
    tmp_path: Path,
    child: _ExitedChild,
    record: ProcessRecord,
) -> ProcessSupervisor:
    supervisor = ProcessSupervisor(tmp_path)
    supervisor._children["live"] = child
    supervisor._save({"live": record})
    return supervisor


def test_unexpected_crash_recovery_requires_running_desired_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor_with_child(tmp_path, _ExitedChild(3), _record())
    recovered: list[str] = []
    restarted = _record(pid=99)
    restarted.restart_count = 1
    monkeypatch.setattr(supervisor_module, "_find_matching_processes", lambda _command: [])
    monkeypatch.setattr(
        supervisor,
        "start_component",
        lambda component, _command: recovered.append(component) or restarted,
    )

    changed = supervisor.monitor_once({"live": COMMAND})

    assert recovered == ["live"]
    assert changed[0].state == "RUNNING"
    assert changed[0].restart_count == 1


def test_intentional_stop_exit_is_not_crashed_or_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    record.state = "STOPPING"
    record.desired_state = "STOPPED"
    record.restart_count = 4
    supervisor = _supervisor_with_child(tmp_path, _ExitedChild(0), record)
    monkeypatch.setattr(
        supervisor_module,
        "_find_matching_processes",
        lambda _command: pytest.fail("intentional stop must not inspect crash survivors"),
    )
    monkeypatch.setattr(
        supervisor,
        "start_component",
        lambda *_args: pytest.fail("intentional stop must not recover"),
    )

    changed = supervisor.monitor_once({"live": COMMAND})

    assert changed[0].state == "STOPPED"
    assert changed[0].restart_count == 4
    assert changed[0].desired_state == "STOPPED"
    assert changed[0].state != "ERROR"
    assert not TelegramControlService._process_crash_notification_required(changed[0])


def test_historical_restart_count_does_not_emit_process_crashed() -> None:
    stopped = _record(pid=None)
    stopped.state = "STOPPED"
    stopped.desired_state = "STOPPED"
    stopped.restart_count = 4
    stopped.last_error = None

    assert not TelegramControlService._process_crash_notification_required(stopped)


def test_crash_transition_still_emits_process_crashed() -> None:
    crashed = _record()
    crashed.state = "CRASHED"
    crashed.restart_count = 4
    assert TelegramControlService._process_crash_notification_required(crashed)

    recovered = _record(pid=99)
    recovered.state = "RUNNING"
    recovered.restart_count = 5
    recovered.last_error = "supervised process exited with code 3; see logs/supervisor_live.log"
    assert TelegramControlService._process_crash_notification_required(recovered)


def test_stop_persists_intent_before_termination_and_records_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()

    class Child:
        pid = 10

        def poll(self):
            return 0

        def wait(self, timeout=0):
            return 0

    child = Child()
    supervisor = ProcessSupervisor(tmp_path)
    supervisor._children["live"] = child
    supervisor._save({"live": record})
    observed: dict[str, object] = {}

    monkeypatch.setattr(supervisor_module, "_verified_owned_pids", lambda *_args: [10])

    def terminate(_owned, _child):
        persisted = supervisor._load()["live"]
        observed["desired_state_at_termination"] = persisted.desired_state
        observed["state_at_termination"] = persisted.state

    monkeypatch.setattr(supervisor_module, "_terminate_owned_processes", terminate)
    monkeypatch.setattr(supervisor_module, "_wait_for_pids_to_exit", lambda *_args, **_kwargs: True)

    result = supervisor.stop_component(
        "live", operation_id="restart-test-id", lifecycle_operation="restart"
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "supervisor_control_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert observed == {
        "desired_state_at_termination": "STOPPED",
        "state_at_termination": "STOPPING",
    }
    assert result.state == "STOPPED"
    assert result.restart_count == 0
    assert all(event["details"]["operation_id"] == "restart-test-id" for event in events)
    assert [event["stage"] for event in events] == [
        "stop_intent_persisted",
        "termination_started",
        "termination_result",
    ]
    assert events[-1]["details"]["verified_stopped"] is True


def test_restart_diagnostics_have_one_operation_id_and_abort_without_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = object.__new__(TelegramControlService)
    service.project_root = tmp_path
    service._operation_lock = asyncio.Lock()
    service.supervisor = SimpleNamespace(
        status=lambda: {
            "api": SimpleNamespace(state="DEGRADED"),
            "live": SimpleNamespace(state="STOPPED"),
        }
    )
    started = False

    async def stop(_self=None, **_kwargs):
        return "stop incomplete"

    async def start(_self=None):
        nonlocal started
        started = True
        return "must not start"

    service._stop_infrastructure_locked = stop
    service._start_infrastructure_locked = start

    response = asyncio.run(service._dispatch("/restart"))
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "supervisor_control_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    restart_events = [event for event in events if event["operation"] == "/restart"]
    operation_ids = {
        event["details"].get("operation_id")
        for event in restart_events
        if event["details"].get("operation_id")
    }

    assert "RESTART ABORTED" in response
    assert not started
    assert len(operation_ids) == 1
    assert {event["stage"] for event in restart_events} == {
        "restart_started",
        "restart_stop_verified",
        "restart_aborted",
    }


def test_stop_during_watchdog_polling_cannot_resurrect_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    record.state = "STOPPING"
    record.desired_state = "STOPPED"
    supervisor = _supervisor_with_child(tmp_path, _ExitedChild(None), record)
    monkeypatch.setattr(
        supervisor,
        "start_component",
        lambda *_args: pytest.fail("stop intent must suppress watchdog recovery"),
    )

    changed = supervisor.monitor_once({"live": COMMAND})
    status = supervisor.status()["live"]

    assert changed == ()
    assert status.state == "STOPPING"
    assert status.desired_state == "STOPPED"


def test_restart_starts_only_after_stop_reaches_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = ProcessSupervisor(tmp_path)
    order: list[str] = []
    stopped = _record()

    def stop(_component: str) -> ProcessRecord:
        order.append("stop")
        stopped.state = "STOPPED"
        stopped.desired_state = "STOPPED"
        return stopped

    def start(_component: str, _command: list[str]) -> ProcessRecord:
        assert stopped.state == "STOPPED"
        order.append("start")
        return _record(pid=99)

    monkeypatch.setattr(supervisor, "stop_component", stop)
    monkeypatch.setattr(supervisor, "start_component", start)

    result = supervisor.restart_component("live", COMMAND)

    assert order == ["stop", "start"]
    assert result.pid == 99


def test_start_rollback_is_empty_when_no_component_was_spawned() -> None:
    service = object.__new__(TelegramControlService)
    calls: list[str] = []
    service.supervisor = SimpleNamespace(stop_component=lambda component: calls.append(component))

    service._stop_failed_start([])

    assert calls == []
    assert service._start_spawned_by_request("api", {}, SimpleNamespace(state="RUNNING")) is False


def test_startup_health_rejects_pre_start_live_and_mt5_rows(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'health.db').as_posix()}")
    database.create_schema()
    try:
        old = datetime.now(UTC) - timedelta(minutes=1)
        with database.session() as session:
            session.add_all([
                SystemHealthRecord(component="live_runtime", timestamp=old, status="CONNECTED"),
                SystemHealthRecord(component="mt5", timestamp=old, status="CONNECTED"),
            ])
        service = TelegramControlService(Settings(), tmp_path, database=database)
        boundary = datetime.now(UTC)

        assert (
            service._latest_service_state("live_runtime", minimum_timestamp=boundary)
            == "UNKNOWN"
        )
        assert service._latest_service_state("mt5", minimum_timestamp=boundary) == "UNKNOWN"
    finally:
        database.dispose()


def test_api_supervisor_health_does_not_trust_running_registry_without_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    now = datetime.now(UTC).isoformat()
    (data / "process_registry.json").write_text(json.dumps({
        "api": {"state": "RUNNING", "last_heartbeat": now},
        "live": {"state": "RUNNING", "last_heartbeat": now},
    }), encoding="utf-8")
    monkeypatch.setattr(api_module, "record_process_is_alive", lambda _record: False)

    assert _supervisor_health_state(tmp_path, datetime.now(UTC), 30) == "DEGRADED"
