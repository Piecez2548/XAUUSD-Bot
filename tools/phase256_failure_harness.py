"""Disposable Phase 2.5.6 failure-path acceptance harness.

The harness uses only temporary registries, temporary worker scripts, isolated
SQLite databases, and unique command markers. It never uses the production
registry, API port, Telegram, MT5, Forward Shadow, or broker execution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import services.control as control_module  # noqa: E402
import services.supervisor as supervisor_module  # noqa: E402
from config.settings import Settings  # noqa: E402
from persistence.database import Database  # noqa: E402
from services.control import TelegramControlService  # noqa: E402
from services.supervisor import (  # noqa: E402
    ProcessIdentity,
    ProcessRecord,
    ProcessSupervisor,
)

TIMEOUT_SECONDS = 8.0


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _identity_payload(identity: ProcessIdentity | None) -> dict[str, object] | None:
    if identity is None:
        return None
    return {
        "pid": identity.pid,
        "parent_pid": identity.parent_pid,
        "creation_time": identity.creation_time,
        "executable": identity.executable,
        "command_line": identity.command_line,
    }


def _record_payload(record: ProcessRecord | None) -> dict[str, object] | None:
    return None if record is None else asdict(record)


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_events(root: Path) -> list[dict[str, object]]:
    path = root / "logs" / "supervisor_control_diagnostics.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class DisposableEnvironment:
    """One isolated scenario root and its verified disposable processes."""

    def __init__(self, evidence_root: Path, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self.root = evidence_root / scenario_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.worker = self.root / "disposable_worker.py"
        worker_source = "import time\nwhile True:\n    time.sleep(1)\n"
        self.worker.write_text(worker_source, encoding="utf-8")
        self.main = self.root / "main.py"
        self.main.write_text(worker_source, encoding="utf-8")
        self.registry_path = self.root / "data" / "process_registry.json"
        self.lock_path = self.root / "data" / "supervisor.lock"
        self.supervisor = ProcessSupervisor(
            self.root,
            registry_path=self.registry_path,
            lock_path=self.lock_path,
            max_restarts=2,
            restart_window_seconds=60,
        )
        self.python = supervisor_module.resolve_supervised_python()
        self.marker = f"phase256-{uuid4().hex}"
        self.raw: list[tuple[subprocess.Popen[bytes], list[str], object]] = []

    def command(self, component: str, marker: str | None = None) -> list[str]:
        suffix = marker or component
        return [
            self.python,
            str(self.worker),
            "--component",
            component,
            "--marker",
            f"{self.marker}-{suffix}",
        ]

    def spawn_raw(self, command: list[str]) -> subprocess.Popen[bytes]:
        log_path = self.root / "logs" / f"raw-{len(self.raw)}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        process = subprocess.Popen(
            command,
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=supervisor_module._subprocess_creation_flags(),
        )
        self.raw.append((process, command, handle))
        self.wait_for_command(command)
        return process

    def wait_for_command(self, command: list[str]) -> None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if supervisor_module._matching_command_roots(
                supervisor_module._process_snapshot(), command
            ):
                return
            time.sleep(0.05)
        raise AssertionError(f"disposable command did not become observable: {command}")

    def topology(self, command: list[str]) -> list[dict[str, object]]:
        snapshot = supervisor_module._process_snapshot()
        identities: dict[int, ProcessIdentity] = {}
        for root in supervisor_module._matching_command_roots(snapshot, command):
            for pid in supervisor_module._matching_descendants(root, snapshot, command):
                if pid in snapshot:
                    identities[pid] = snapshot[pid]
        return [
            _identity_payload(identities[pid])  # type: ignore[arg-type]
            for pid in sorted(identities)
        ]

    def roots(self, command: list[str]) -> list[dict[str, object]]:
        snapshot = supervisor_module._process_snapshot()
        return [
            _identity_payload(snapshot[pid])  # type: ignore[arg-type]
            for pid in supervisor_module._matching_command_roots(snapshot, command)
        ]

    def registry(self) -> object:
        return _read_json(self.registry_path, {})

    def unrelated_state(self, command: list[str]) -> dict[str, object] | None:
        snapshot = supervisor_module._process_snapshot()
        roots = supervisor_module._matching_command_roots(snapshot, command)
        if not roots:
            return None
        return _identity_payload(snapshot[roots[0]])

    def cleanup(self) -> dict[str, object]:
        errors: list[str] = []
        records = self.supervisor._load()
        for component in ("live", "api"):
            try:
                self.supervisor.stop_component(component)
            except Exception as exc:  # pragma: no cover - final safety path
                command = list(records.get(component).command) if component in records else []
                if command and self.topology(command):
                    snapshot = supervisor_module._process_snapshot()
                    owned: set[int] = set()
                    for root in supervisor_module._matching_command_roots(snapshot, command):
                        owned.update(
                            supervisor_module._matching_descendants(root, snapshot, command)
                        )
                    try:
                        if owned:
                            supervisor_module._terminate_owned_processes(
                                sorted(owned, reverse=True), None
                            )
                            supervisor_module._wait_for_pids_to_exit(
                                sorted(owned), timeout=TIMEOUT_SECONDS
                            )
                    except BaseException:
                        pass
                if command and not self.topology(command):
                    continue
                errors.append(f"{component}: {type(exc).__name__}: {exc}")

        for process, command, handle in self.raw:
            try:
                if process.poll() is None:
                    snapshot = supervisor_module._process_snapshot()
                    roots = supervisor_module._matching_command_roots(snapshot, command)
                    owned: set[int] = set()
                    for root in roots:
                        owned.update(
                            supervisor_module._matching_descendants(root, snapshot, command)
                        )
                    if owned:
                        supervisor_module._terminate_owned_processes(
                            sorted(owned, reverse=True), None
                        )
                        supervisor_module._wait_for_pids_to_exit(
                            sorted(owned), timeout=TIMEOUT_SECONDS
                        )
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=TIMEOUT_SECONDS)
            except Exception as exc:  # pragma: no cover - final safety path
                if self.topology(command):
                    errors.append(f"raw-{process.pid}: {type(exc).__name__}: {exc}")
            finally:
                handle.close()

        leftovers = []
        snapshot = supervisor_module._process_snapshot()
        for identity in snapshot.values():
            if self.marker.casefold() in identity.command_line.casefold():
                leftovers.append(_identity_payload(identity))
        result = {"errors": errors, "leftovers": leftovers, "clean": not errors and not leftovers}
        (self.root / "cleanup.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return result


def _restart_service(env: DisposableEnvironment) -> TelegramControlService:
    service = object.__new__(TelegramControlService)
    service.project_root = env.root
    service.supervisor = env.supervisor
    service._operation_lock = asyncio.Lock()
    return service


def _write_result(env: DisposableEnvironment, payload: dict[str, object]) -> dict[str, object]:
    payload["scenario_id"] = env.scenario_id
    payload["registry_path"] = str(env.registry_path)
    payload["evidence_root"] = str(env.root)
    payload["events"] = _read_events(env.root)
    (env.root / "evidence.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return payload


def _run_incomplete_stop(
    env: DisposableEnvironment,
    blocked_component: str,
) -> dict[str, object]:
    api_command = env.command("api")
    live_command = env.command("live")
    unrelated_command = env.command("unrelated")
    env.supervisor.start_component("api", api_command)
    env.supervisor.start_component("live", live_command)
    env.spawn_raw(unrelated_command)
    initial = {
        "registry": env.registry(),
        "api_topology": env.topology(api_command),
        "live_topology": env.topology(live_command),
    }
    unrelated_before = env.unrelated_state(unrelated_command)
    initial_counts = {
        name: env.supervisor._load()[name].restart_count for name in ("api", "live")
    }
    original = supervisor_module._verified_owned_pids

    def injected(record, child):
        if record.component == blocked_component:
            return None
        return original(record, child)

    service = _restart_service(env)
    started = False

    async def unexpected_start():
        nonlocal started
        started = True
        raise AssertionError("restart startup must not be reached after incomplete stop")

    service._start_infrastructure_locked = unexpected_start
    with patch.object(supervisor_module, "_verified_owned_pids", injected):
        response = asyncio.run(service._dispatch("/restart"))
        changed = env.supervisor.monitor_once({"api": api_command, "live": live_command})
        records = env.supervisor.status()
    notifications = [
        record.component
        for record in changed
        if TelegramControlService._process_crash_notification_required(record)
    ]
    after = {
        "registry": env.registry(),
        "api_topology": env.topology(api_command),
        "live_topology": env.topology(live_command),
        "records": {name: _record_payload(records.get(name)) for name in ("api", "live")},
        "changed": [_record_payload(record) for record in changed],
    }
    unrelated_after = env.unrelated_state(unrelated_command)
    final_counts = {
        name: records[name].restart_count for name in ("api", "live")
    }
    _require("RESTART ABORTED" in response, f"unexpected response: {response}")
    _require(not started, "restart attempted startup after incomplete stop")
    _require(
        after["records"][blocked_component]["state"] == "STOPPING",
        "blocked component was falsely STOPPED",
    )  # type: ignore[index]
    _require(not notifications, "intentional stop produced PROCESS_CRASHED notification")
    _require(initial_counts == final_counts, "intentional stop changed restart_count")
    _require(unrelated_before == unrelated_after, "unrelated process identity changed")
    return _write_result(
        env,
        {
            "operation_id": next(
                (
                    event["details"]["operation_id"]
                    for event in _read_events(env.root)
                    if event.get("stage") == "restart_started"
                ),
                None,
            ),
            "blocked_component": blocked_component,
            "response": response,
            "initial": initial,
            "final_before_cleanup": after,
            "unrelated_before": unrelated_before,
            "unrelated_after": unrelated_after,
            "notifications": notifications,
            "restart_counts_unchanged": initial_counts == final_counts,
            "replacement_started": started,
        },
    )


def _run_ambiguous(env: DisposableEnvironment) -> dict[str, object]:
    command = env.command("ambiguous", marker="same-topology")
    unrelated_command = env.command("unrelated")
    first = env.spawn_raw(command)
    second = env.spawn_raw(command)
    env.spawn_raw(unrelated_command)
    before = env.topology(command)
    before_roots = env.roots(command)
    result = env.supervisor.start_component("api", command)
    restart = env.supervisor.restart_component("api", command)
    after = env.topology(command)
    after_roots = env.roots(command)
    unrelated_before = env.unrelated_state(unrelated_command)
    unrelated_after = env.unrelated_state(unrelated_command)
    _require(result.state == "DEGRADED", f"ambiguous start state was {result.state}")
    _require("multiple independent" in (result.last_error or ""), "ambiguity reason missing")
    _require(restart.state == "DEGRADED", "ambiguous restart did not fail closed")
    _require(
        len(before_roots) == 2 and len(after_roots) == 2,
        "ambiguous topology was adopted or changed",
    )
    _require(first.poll() is None and second.poll() is None, "ambiguous process was terminated")
    _require(unrelated_before == unrelated_after, "unrelated process identity changed")
    return _write_result(
        env,
        {
            "initial_topology": before,
            "final_topology_before_cleanup": after,
            "initial_roots": before_roots,
            "final_roots_before_cleanup": after_roots,
            "start_record": _record_payload(result),
            "restart_record": _record_payload(restart),
            "third_replacement_created": len(after) != 2,
            "unrelated_before": unrelated_before,
            "unrelated_after": unrelated_after,
        },
    )


def _run_identity_rejection(env: DisposableEnvironment) -> dict[str, object]:
    unrelated_command = env.command("unrelated")
    env.spawn_raw(unrelated_command)
    cases: list[dict[str, object]] = []
    for label, creation_time in (("missing-creation-time", None), ("pid-reuse", "old-create-time")):
        command = env.command("identity", marker=label)
        process = env.spawn_raw(command)
        identity = supervisor_module._process_snapshot()[process.pid]
        record = ProcessRecord(
            component="api",
            pid=process.pid,
            state="RUNNING",
            started_at="2026-09-23T00:00:00+00:00",
            last_heartbeat="2026-09-23T00:00:00+00:00",
            exit_code=None,
            desired_state="RUNNING",
            command=tuple(command),
            process_create_time=creation_time,
            process_tree=(process.pid,),
            process_identities=((process.pid, creation_time),) if creation_time else (),
        )
        env.supervisor._save({"api": record})
        stopped = env.supervisor.stop_component("api", operation_id=f"identity-{label}")
        cases.append(
            {
                "label": label,
                "identity_before": _identity_payload(identity),
                "record_after": _record_payload(stopped),
                "process_alive_after_rejection": process.poll() is None,
                "topology_after_rejection": env.topology(command),
            }
        )
        _require(stopped.state == "DEGRADED", f"{label} was not degraded")
        _require(process.poll() is None, f"{label} process was terminated")
    unrelated_before = env.unrelated_state(unrelated_command)
    unrelated_after = env.unrelated_state(unrelated_command)
    _require(unrelated_before == unrelated_after, "unrelated process identity changed")
    return _write_result(
        env,
        {"cases": cases, "unrelated_before": unrelated_before, "unrelated_after": unrelated_after},
    )


def _make_database(env: DisposableEnvironment) -> Database:
    database = Database(f"sqlite:///{(env.root / 'control.db').as_posix()}")
    database.create_schema()
    return database


def _run_pre_popen(env: DisposableEnvironment) -> dict[str, object]:
    secret = "phase256-secret-token"
    database = _make_database(env)

    class FailingBootstrap:
        async def ensure_ready(self, _database):
            raise RuntimeError(f"controlled bootstrap failure token={secret}")

    service = TelegramControlService(
        Settings(
            database_url=f"sqlite:///{(env.root / 'control.db').as_posix()}",
            mt5_login=1,
            mt5_server="phase256-disposable",
            mt5_password=secret,
            telegram_bot_token=secret,
        ),
        env.root,
        database=database,
        supervisor=env.supervisor,
        mt5_bootstrap=FailingBootstrap(),
    )
    try:
        with patch.object(control_module, "migrate_database", lambda *_args: None):
            response = asyncio.run(service._start_infrastructure_locked())
        events = _read_events(env.root)
        diagnostic_text = json.dumps(events, ensure_ascii=False)
        _require(response.startswith("🟠 START INCOMPLETE"), "pre-Popen response was not safe")
        _require(
            any(event.get("stage") == "mt5_readiness" for event in events),
            "failed stage missing",
        )
        _require(secret not in diagnostic_text, "secret leaked into diagnostics")
        _require(env.supervisor._load() == {}, "pre-Popen failure created a registry child")
        return _write_result(
            env,
            {
                "response": response,
                "diagnostic_events": events,
                "secret_redacted": secret not in diagnostic_text,
                "registry_after": env.registry(),
                "topology_after": [],
            },
        )
    finally:
        database.dispose()


def _run_post_spawn(env: DisposableEnvironment) -> dict[str, object]:
    database = _make_database(env)
    api_command = [env.python, str(env.main), "server"]
    preexisting = env.supervisor.start_component("api", api_command)
    preexisting_pid = preexisting.pid

    class ReadyBootstrap:
        async def ensure_ready(self, _database):
            return SimpleNamespace(ready=True, launch_state="REUSED", pid=None, checks={})

    service = TelegramControlService(
        Settings(database_url=f"sqlite:///{(env.root / 'control.db').as_posix()}"),
        env.root,
        database=database,
        supervisor=env.supervisor,
        mt5_bootstrap=ReadyBootstrap(),
    )

    async def unhealthy_monitoring():
        return False

    async def failed_verification():
        return {
            "complete": False,
            "checks": {
                "api": "OK",
                "live_runtime": "FAILED_INJECTED",
                "mt5": "CONNECTED",
            },
        }

    try:
        service._monitoring_is_healthy = unhealthy_monitoring
        service._wait_for_startup_verification = failed_verification
        with (
            patch.object(control_module, "migrate_database", lambda *_args: None),
            patch.object(control_module, "resolve_supervised_python", lambda: env.python),
        ):
            response = asyncio.run(service._start_infrastructure_locked())
        records = env.supervisor.status()
        events = _read_events(env.root)
        rollback = [event for event in events if event.get("stage") == "startup_rollback"]
        verification = [event for event in events if event.get("stage") == "startup_verification"]
        _require(response.startswith("🟠 START INCOMPLETE"), "post-spawn failure response missing")
        _require(preexisting_pid == records["api"].pid, "pre-existing API was changed")
        _require(records["api"].state == "RUNNING", "pre-existing API was not preserved")
        _require(records["live"].state == "STOPPED", "spawned Live was not rolled back")
        _require(
            verification
            and verification[-1]["details"]["checks"]["live_runtime"] == "FAILED_INJECTED",
            "false check missing",
        )
        _require(
            rollback and rollback[-1]["details"]["rollback_target"] == "live",
            "rollback target missing",
        )
        return _write_result(
            env,
            {
                "response": response,
                "preexisting_api_pid": preexisting_pid,
                "final_registry": env.registry(),
                "final_records": {name: _record_payload(records[name]) for name in ("api", "live")},
                "startup_verification": verification,
                "startup_rollback": rollback,
                "preexisting_component_untouched": records["api"].pid == preexisting_pid,
            },
        )
    finally:
        database.dispose()


def run_harness(evidence_root: Path) -> dict[str, object]:
    if os.name != "nt":
        raise RuntimeError("Phase 2.5.6 harness requires Windows process identity inspection")
    scenarios = [
        ("api-incomplete-stop", lambda env: _run_incomplete_stop(env, "api")),
        ("live-incomplete-stop", lambda env: _run_incomplete_stop(env, "live")),
        ("ambiguous-topology", _run_ambiguous),
        ("identity-rejection", _run_identity_rejection),
        ("pre-popen-start-failure", _run_pre_popen),
        ("post-spawn-verification-failure", _run_post_spawn),
    ]
    results: list[dict[str, object]] = []
    for scenario_id, runner in scenarios:
        env = DisposableEnvironment(evidence_root, scenario_id)
        result: dict[str, object]
        try:
            result = runner(env)
        except Exception as exc:
            result = _write_result(
                env,
                {"passed": False, "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        finally:
            cleanup = env.cleanup()
            result["cleanup"] = cleanup
            result["passed"] = bool(result.get("passed", True)) and bool(cleanup["clean"])
            (env.root / "evidence.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
        results.append(result)
    summary = {
        "phase": "2.5.6",
        "evidence_root": str(evidence_root),
        "scenarios": results,
        "all_passed": all(bool(item.get("passed")) for item in results),
    }
    (evidence_root / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="isolated evidence directory; defaults to a new system temp directory",
    )
    args = parser.parse_args()
    evidence_root = args.evidence_dir or Path(tempfile.mkdtemp(prefix="xauusd-phase256-"))
    evidence_root.mkdir(parents=True, exist_ok=True)
    summary = run_harness(evidence_root)
    print(json.dumps(summary, indent=2, ensure_ascii=True, default=str))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
