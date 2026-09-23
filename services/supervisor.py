"""Conservative local process supervision for the read-only observatory."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(slots=True)
class ProcessRecord:
    component: str
    pid: int | None
    state: str
    started_at: str | None
    last_heartbeat: str | None
    exit_code: int | None
    desired_state: str = "STOPPED"
    restart_count: int = 0
    command: tuple[str, ...] = ()
    last_error: str | None = None
    process_create_time: str | None = None
    parent_pid: int | None = None
    process_tree: tuple[int, ...] = ()
    process_identities: tuple[tuple[int, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """OS identity used to distinguish a supervised process from a reused PID."""

    pid: int
    parent_pid: int | None
    creation_time: str | None
    executable: str | None
    command_line: str


class SupervisorOperationLock:
    """Atomic lock file; stale locks are recoverable only when owner is dead."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    def __enter__(self) -> SupervisorOperationLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            self._owned = True
            return self
        except FileExistsError as exc:
            owner = _read_pid(self.path)
            if owner is None or not _pid_alive(owner):
                with _suppress_os_error():
                    self.path.unlink()
                return self.__enter__()
            raise RuntimeError("supervisor operation already in progress") from exc

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._owned:
            with _suppress_os_error():
                self.path.unlink()
            self._owned = False


class ProcessSupervisor:
    """Own supervised API/live child topologies and persist lifecycle state."""

    STOP_TIMEOUT_SECONDS = 15.0
    IDENTITY_STABILIZATION_SECONDS = 2.0
    IDENTITY_POLL_SECONDS = 0.05

    def __init__(
        self,
        project_root: Path,
        *,
        registry_path: Path | None = None,
        lock_path: Path | None = None,
        max_restarts: int = 3,
        restart_window_seconds: float = 600,
    ) -> None:
        self.project_root = project_root
        self.registry_path = registry_path or project_root / "data" / "process_registry.json"
        self.lock_path = lock_path or project_root / "data" / "supervisor.lock"
        self.max_restarts = max_restarts
        self.restart_window = timedelta(seconds=restart_window_seconds)
        self._children: dict[str, subprocess.Popen[Any]] = {}
        self._diagnostic_handles: dict[str, Any] = {}
        self._restart_times: dict[str, list[datetime]] = {}
        self._last_start_spawned: dict[str, bool] = {}
        self._lifecycle_context: dict[str, tuple[str, str]] = {}
        self._mutex = RLock()

    def start_component(self, component: str, command: list[str]) -> ProcessRecord:
        with self._mutex, SupervisorOperationLock(self.lock_path):
            self._lifecycle_context.pop(component, None)
            records = self._load()
            existing = records.get(component)
            if existing is not None:
                existing.desired_state = "RUNNING"
            child = self._children.get(component)
            if child is not None and child.poll() is None and (
                existing is None or existing.command == tuple(command)
            ):
                record = existing or self._new_record(component, command)
                record.desired_state = "RUNNING"
                verified = self._refresh_identity(record, child.pid, command)
                record.state = "RUNNING" if verified or os.name != "nt" else "DEGRADED"
                record.last_error = (
                    None if record.state == "RUNNING" else "process identity could not be verified"
                )
                self._last_start_spawned[component] = False
                records[component] = record
                self._save(records)
                return record
            if child is not None and child.poll() is None:
                record = existing or self._new_record(component, command)
                record.desired_state = "RUNNING"
                record.state = "DEGRADED"
                record.last_error = "component already has a live process with a different command"
                self._last_start_spawned[component] = False
                records[component] = record
                self._save(records)
                return record
            if (
                existing
                and existing.pid
                and existing.command == tuple(command)
                and _record_matches_process(existing)
            ):
                self._refresh_identity(existing, existing.pid, command)
                existing.desired_state = "RUNNING"
                existing.state = "RUNNING"
                existing.last_error = None
                self._last_start_spawned[component] = False
                records[component] = existing
                self._save(records)
                return existing
            if existing and existing.pid and _pid_alive(existing.pid):
                existing.desired_state = "RUNNING"
                existing.state = "DEGRADED"
                existing.last_error = "live PID identity could not be verified"
                self._last_start_spawned[component] = False
                records[component] = existing
                self._save(records)
                return existing

            # A prior supervisor instance may have lost its registry update
            # while its child is still alive.  Match by command and collapse a
            # launcher/interpreter parent-child tree into one root identity.
            matches = _find_matching_processes(command)
            if len(matches) > 1:
                record = existing or self._new_record(component, command)
                record.desired_state = "RUNNING"
                record.state = "DEGRADED"
                record.last_error = "multiple independent matching process trees detected"
                self._last_start_spawned[component] = False
                records[component] = record
                self._save(records)
                return record
            if matches:
                if (
                    existing is not None
                    and existing.pid in matches
                    and not _record_matches_process(existing)
                ):
                    existing.desired_state = "RUNNING"
                    existing.state = "DEGRADED"
                    existing.last_error = "matching PID identity could not be verified"
                    self._last_start_spawned[component] = False
                    records[component] = existing
                    self._save(records)
                    return existing
                adopted_pid = matches[0]
                record = existing or self._new_record(component, command)
                record.desired_state = "RUNNING"
                record.pid = adopted_pid
                verified = self._refresh_identity(record, adopted_pid, command)
                record.state = "RUNNING" if verified else "DEGRADED"
                record.exit_code = None
                record.last_error = None if verified else "process identity could not be verified"
                self._last_start_spawned[component] = False
                records[component] = record
                self._save(records)
                return record

            started = datetime.now(UTC)
            flags = _subprocess_creation_flags()
            diagnostic_handle = self._open_diagnostics(component)
            try:
                child = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=diagnostic_handle,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    creationflags=flags,
                )
            except (OSError, subprocess.SubprocessError, SystemError) as exc:
                self._close_diagnostics(component)
                write_supervisor_diagnostic(
                    self.project_root,
                    operation="start_component",
                    stage=f"popen:{component}",
                    exc=exc,
                )
                record = existing or self._new_record(component, command)
                record.desired_state = "RUNNING"
                record.state = "ERROR"
                record.last_error = (
                    f"supervised process launch failed ({type(exc).__name__}); "
                    f"see logs/supervisor_{component}.log"
                )
                self._last_start_spawned[component] = False
                records[component] = record
                self._save(records)
                return record
            self._children[component] = child
            write_supervisor_lifecycle_event(
                self.project_root,
                operation="start_component",
                stage="component_spawned",
                component=component,
                details={
                    "initial_pid": child.pid,
                    "command": tuple(command),
                },
            )
            restart_count = existing.restart_count if existing else 0
            record = ProcessRecord(
                component=component,
                pid=child.pid,
                state="RUNNING",
                started_at=started.isoformat(),
                last_heartbeat=started.isoformat(),
                exit_code=None,
                restart_count=restart_count,
                command=tuple(command),
                last_error=(
                    existing.last_error
                    if existing is not None and existing.state in {"CRASHED", "ERROR"}
                    else None
                ),
            )
            record.desired_state = "RUNNING"
            identity_observed = self._wait_for_identity(child.pid, command)
            verified = self._refresh_identity(record, child.pid, command)
            if not verified and identity_observed:
                deadline = time.monotonic() + self.IDENTITY_POLL_SECONDS * 10
                while not verified and time.monotonic() < deadline:
                    time.sleep(self.IDENTITY_POLL_SECONDS)
                    verified = self._refresh_identity(record, child.pid, command)
            if os.name == "nt" and not verified:
                record.state = "DEGRADED"
                record.last_error = "spawned process identity could not be verified"
            self._last_start_spawned[component] = True
            records[component] = record
            self._save(records)
            write_supervisor_lifecycle_event(
                self.project_root,
                operation="start_component",
                stage="initial_verification",
                component=component,
                details={
                    "initial_pid": child.pid,
                    "identity_observed_during_stabilization": identity_observed,
                    "verification_state": record.state,
                    "verification_failure_reason": record.last_error,
                    "observed_pid": record.pid,
                    "process_tree": record.process_tree,
                    "process_identities": record.process_identities,
                },
            )
            return record

    def stop_component(
        self,
        component: str,
        *,
        operation_id: str | None = None,
        lifecycle_operation: str = "stop",
    ) -> ProcessRecord:
        with self._mutex, SupervisorOperationLock(self.lock_path):
            records = self._load()
            record = records.get(component) or ProcessRecord(
                component=component,
                pid=None,
                state="STOPPED",
                started_at=None,
                last_heartbeat=None,
                exit_code=None,
            )
            diagnostic_operation = f"/{lifecycle_operation}"
            pre_stop_desired_state = record.desired_state
            record.desired_state = "STOPPED"
            if operation_id is not None:
                self._lifecycle_context[component] = (operation_id, lifecycle_operation)
            child = self._children.get(component)
            if record.pid is None and child is None:
                self._close_diagnostics(component)
                self._last_start_spawned[component] = False
                record.state = "STOPPED"
                records[component] = record
                self._save(records)
                write_supervisor_lifecycle_event(
                    self.project_root,
                    operation=diagnostic_operation,
                    stage="stop_intent_persisted",
                    component=component,
                    details={
                        "operation_id": operation_id,
                        "pre_stop_desired_state": pre_stop_desired_state,
                        "persisted_stop_intent": True,
                        "termination_result": "already_stopped",
                        "verified_stopped": True,
                        "final_state": record.state,
                    },
                )
                return record
            record.state = "STOPPING"
            records[component] = record
            self._save(records)
            write_supervisor_lifecycle_event(
                self.project_root,
                operation=diagnostic_operation,
                stage="stop_intent_persisted",
                component=component,
                details={
                    "operation_id": operation_id,
                    "pre_stop_desired_state": pre_stop_desired_state,
                    "persisted_stop_intent": True,
                    "termination_start": "pending",
                    "state": record.state,
                },
            )
            owned = _verified_owned_pids(record, child)
            if owned is None:
                self._last_start_spawned[component] = False
                record.state = "DEGRADED"
                record.last_error = "process identity is unverified; not terminated"
                records[component] = record
                self._save(records)
                write_supervisor_lifecycle_event(
                    self.project_root,
                    operation=diagnostic_operation,
                    stage="termination_result",
                    component=component,
                    details={
                        "operation_id": operation_id,
                        "termination_result": "aborted_unverified_identity",
                        "verified_stopped": False,
                        "final_state": record.state,
                        "abort_reason": record.last_error,
                    },
                )
                return record
            write_supervisor_lifecycle_event(
                self.project_root,
                operation=diagnostic_operation,
                stage="termination_started",
                component=component,
                details={
                    "operation_id": operation_id,
                    "persisted_stop_intent": record.desired_state == "STOPPED",
                    "owned_pids": owned,
                },
            )
            _terminate_owned_processes(owned, child)
            if not _wait_for_pids_to_exit(owned, timeout=self.STOP_TIMEOUT_SECONDS):
                self._last_start_spawned[component] = False
                record.state = "DEGRADED"
                record.last_error = "supervised process did not stop within timeout"
                records[component] = record
                self._save(records)
                write_supervisor_lifecycle_event(
                    self.project_root,
                    operation=diagnostic_operation,
                    stage="termination_result",
                    component=component,
                    details={
                        "operation_id": operation_id,
                        "termination_result": "timeout",
                        "verified_stopped": False,
                        "final_state": record.state,
                        "abort_reason": record.last_error,
                    },
                )
                return record
            if child is not None:
                with _suppress_os_error():
                    record.exit_code = child.wait(timeout=0)
            self._children.pop(component, None)
            self._close_diagnostics(component)
            self._last_start_spawned[component] = False
            record.state = "STOPPED"
            record.pid = None
            record.parent_pid = None
            record.process_create_time = None
            record.process_tree = ()
            record.process_identities = ()
            record.last_heartbeat = datetime.now(UTC).isoformat()
            record.last_error = None
            records[component] = record
            self._save(records)
            write_supervisor_lifecycle_event(
                self.project_root,
                operation=diagnostic_operation,
                stage="termination_result",
                component=component,
                details={
                    "operation_id": operation_id,
                    "termination_result": "stopped",
                    "verified_stopped": True,
                    "final_state": record.state,
                    "exit_code": record.exit_code,
                },
            )
            return record

    def restart_component(self, component: str, command: list[str]) -> ProcessRecord:
        stopped = self.stop_component(component)
        if stopped.state != "STOPPED":
            return stopped
        return self.start_component(component, command)

    def last_start_spawned(self, component: str) -> bool:
        """Return whether the most recent start call created a new process."""

        return self._last_start_spawned.get(component, False)

    def reconcile(self) -> dict[str, ProcessRecord]:
        """Reconcile persisted records with verified current OS state.

        The registry is evidence, not proof of a live process. This
        non-terminating reconciliation clears conclusively absent identities,
        persists the result, and leaves live-but-unverifiable or ambiguous
        identities degraded so callers can fail closed without deleting the
        registry.
        """

        return self.status()

    def monitor_once(self, commands: dict[str, list[str]]) -> tuple[ProcessRecord, ...]:
        with self._mutex:
            records = self._load()
            changed: list[ProcessRecord] = []
            now = datetime.now(UTC)
            for component, child in tuple(self._children.items()):
                record = records.get(component) or self._new_record(
                    component, commands[component]
                )
                code = child.poll()
                if code is None:
                    if record.desired_state != "RUNNING":
                        record.state = "STOPPING"
                        record.last_heartbeat = now.isoformat()
                        records[component] = record
                        self._write_monitor_observation(
                            component,
                            observed_state=record.state,
                            verified_stopped=False,
                            process_present=True,
                        )
                        continue
                    verified = self._refresh_identity(
                        record, child.pid, commands.get(component, list(record.command))
                    )
                    record.state = "RUNNING" if verified or os.name != "nt" else "DEGRADED"
                    record.last_error = (
                        None
                        if record.state == "RUNNING"
                        else "process identity could not be verified"
                    )
                    record.last_heartbeat = now.isoformat()
                    records[component] = record
                    continue
                command = commands.get(component, list(record.command))
                if record.desired_state != "RUNNING":
                    record.exit_code = code
                    record.pid = None
                    record.parent_pid = None
                    record.process_create_time = None
                    record.process_tree = ()
                    record.process_identities = ()
                    record.state = "STOPPED"
                    record.last_error = None
                    self._children.pop(component, None)
                    self._close_diagnostics(component)
                    records[component] = record
                    self._write_monitor_observation(
                        component,
                        observed_state=record.state,
                        verified_stopped=True,
                        process_present=False,
                    )
                    changed.append(record)
                    continue
                survivors = _find_matching_processes(command)
                if len(survivors) == 1:
                    # A Python launcher exited while its interpreter child
                    # survived. Adopt the child rather than restarting a
                    # second polling loop.
                    record.pid = survivors[0]
                    self._refresh_identity(record, survivors[0], command)
                    record.state = "RUNNING"
                    record.last_heartbeat = now.isoformat()
                    self._children.pop(component, None)
                    records[component] = record
                    changed.append(record)
                    continue
                record.exit_code = code
                record.pid = None
                record.process_tree = ()
                record.process_identities = ()
                record.state = "CRASHED"
                record.last_error = self._exit_diagnostic(component, code)
                history = [
                    item
                    for item in self._restart_times.get(component, [])
                    if now - item <= self.restart_window
                ]
                self._restart_times[component] = history
                self._children.pop(component, None)
                self._close_diagnostics(component)
                if len(history) < self.max_restarts and component in commands:
                    history.append(now)
                    record.restart_count += 1
                    records[component] = record
                    self._save(records)
                    changed.append(self.start_component(component, commands[component]))
                else:
                    record.state = "ERROR"
                    record.last_error = "restart limit exceeded"
                    records[component] = record
                    changed.append(record)
            self._save(records)
            return tuple(changed)

    def status(self) -> dict[str, ProcessRecord]:
        with self._mutex:
            records = self._load()
            for component, child in tuple(self._children.items()):
                record = records.get(component) or self._new_record(component, [])
                if child.poll() is None:
                    if record.desired_state != "RUNNING":
                        record.state = "STOPPING"
                        record.last_heartbeat = datetime.now(UTC).isoformat()
                        records[component] = record
                        self._write_monitor_observation(
                            component,
                            observed_state=record.state,
                            verified_stopped=False,
                            process_present=True,
                        )
                        continue
                    verified = self._refresh_identity(record, child.pid, list(record.command))
                    record.state = "RUNNING" if verified or os.name != "nt" else "DEGRADED"
                    record.last_error = (
                        None
                        if record.state == "RUNNING"
                        else "process identity could not be verified"
                    )
                    record.last_heartbeat = datetime.now(UTC).isoformat()
                elif record.desired_state != "RUNNING":
                    record.exit_code = child.poll()
                    record.state = "STOPPED"
                    record.pid = None
                    record.parent_pid = None
                    record.process_create_time = None
                    record.process_tree = ()
                    record.process_identities = ()
                    self._children.pop(component, None)
                    self._close_diagnostics(component)
                    self._write_monitor_observation(
                        component,
                        observed_state=record.state,
                        verified_stopped=True,
                        process_present=False,
                    )
                elif _record_matches_process(record):
                    self._refresh_identity(record, record.pid or child.pid, list(record.command))
                    record.state = "RUNNING"
                    record.last_heartbeat = datetime.now(UTC).isoformat()
                    self._children.pop(component, None)
                else:
                    record.exit_code = child.poll()
                    record.state = "CRASHED"
                    record.last_error = self._exit_diagnostic(component, record.exit_code)
                    record.pid = None
                    record.process_tree = ()
                    record.process_identities = ()
                    self._children.pop(component, None)
                    self._close_diagnostics(component)
                records[component] = record
            for component, record in records.items():
                if record.pid is None or component in self._children:
                    continue
                if record.desired_state != "RUNNING":
                    if _record_matches_process(record):
                        record.state = "STOPPING"
                    elif _pid_alive(record.pid):
                        record.state = "DEGRADED"
                        record.last_error = "stop requested but process identity is unverified"
                    else:
                        record.state = "STOPPED"
                        self._close_diagnostics(component)
                        record.pid = None
                        record.parent_pid = None
                        record.process_create_time = None
                        record.process_tree = ()
                        record.process_identities = ()
                    continue
                if _record_matches_process(record):
                    self._refresh_identity(record, record.pid, list(record.command))
                    record.state = "RUNNING"
                    record.last_heartbeat = datetime.now(UTC).isoformat()
                elif _pid_alive(record.pid):
                    record.state = "DEGRADED"
                    record.last_error = "live PID identity could not be verified"
                elif record.state in {"RUNNING", "STARTING", "STOPPING"}:
                    record.state = "STOPPED"
                    record.pid = None
                    record.parent_pid = None
                    record.process_create_time = None
                    record.process_tree = ()
                    record.process_identities = ()
                    record.last_error = "process no longer exists"
                    self._close_diagnostics(component)
            self._save(records)
            return records

    def _write_monitor_observation(
        self,
        component: str,
        *,
        observed_state: str,
        verified_stopped: bool,
        process_present: bool,
    ) -> None:
        context = self._lifecycle_context.get(component)
        if context is None:
            return
        operation_id, lifecycle_operation = context
        write_supervisor_lifecycle_event(
            self.project_root,
            operation=f"/{lifecycle_operation}",
            stage="monitor_observation",
            component=component,
            details={
                "operation_id": operation_id,
                "desired_state": "STOPPED",
                "observed_state": observed_state,
                "process_present": process_present,
                "verified_stopped": verified_stopped,
            },
        )

    @staticmethod
    def _new_record(component: str, command: list[str]) -> ProcessRecord:
        now = datetime.now(UTC).isoformat()
        return ProcessRecord(
            component=component,
            pid=None,
            state="STOPPED",
            started_at=None,
            last_heartbeat=now,
            exit_code=None,
            command=tuple(command),
        )

    def _open_diagnostics(self, component: str):
        path = self.project_root / "logs" / f"supervisor_{component}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8", errors="replace", buffering=1)
        self._diagnostic_handles[component] = handle
        return handle

    def _close_diagnostics(self, component: str) -> None:
        handle = self._diagnostic_handles.pop(component, None)
        if handle is not None:
            with _suppress_os_error():
                handle.flush()
                handle.close()

    @staticmethod
    def _exit_diagnostic(component: str, exit_code: int | None) -> str:
        return (
            f"supervised process exited with code {exit_code}; "
            f"see logs/supervisor_{component}.log"
        )

    def _refresh_identity(self, record: ProcessRecord, pid: int, command: list[str]) -> bool:
        snapshot = _process_snapshot()
        identity = snapshot.get(pid)
        root_pid = pid if identity and _command_matches(identity.command_line, command) else None
        if root_pid is None:
            verified = _verified_stored_pids(record, snapshot, command)
            if verified:
                root_pid = next(
                    (
                        candidate
                        for candidate, _ in record.process_identities
                        if candidate in verified
                    ),
                    min(verified),
                )
        if root_pid is None:
            roots = _matching_command_roots(snapshot, command)
            if len(roots) == 1:
                root_pid = roots[0]
        if root_pid is None:
            return False
        identity = snapshot.get(root_pid)
        record.pid = root_pid
        record.command = tuple(command)
        record.process_create_time = (
            identity.creation_time if identity else record.process_create_time
        )
        record.parent_pid = identity.parent_pid if identity else record.parent_pid
        owned = _matching_descendants(root_pid, snapshot, command)
        owned.update(_verified_stored_pids(record, snapshot, command))
        record.process_tree = tuple(sorted(owned)) or (root_pid,)
        record.process_identities = tuple(
            (owned_pid, snapshot[owned_pid].creation_time)
            for owned_pid in sorted(owned)
            if owned_pid in snapshot
        )
        record.last_heartbeat = datetime.now(UTC).isoformat()
        return True

    def _wait_for_identity(self, pid: int, command: list[str]) -> bool:
        if os.name != "nt":
            return False
        deadline = time.monotonic() + self.IDENTITY_STABILIZATION_SECONDS
        while time.monotonic() < deadline:
            snapshot = _process_snapshot()
            if (
                pid in snapshot
                and _command_matches(snapshot[pid].command_line, command)
            ) or len(_matching_command_roots(snapshot, command)) == 1:
                return True
            time.sleep(self.IDENTITY_POLL_SECONDS)
        return False

    def _load(self) -> dict[str, ProcessRecord]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        records: dict[str, ProcessRecord] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            payload = dict(value)
            if "desired_state" not in payload:
                payload["desired_state"] = (
                    "RUNNING"
                    if payload.get("state") in {"RUNNING", "STARTING"}
                    else "STOPPED"
                )
            payload["command"] = tuple(payload.get("command") or ())
            payload["process_tree"] = tuple(payload.get("process_tree") or ())
            payload["process_identities"] = tuple(
                (int(item[0]), item[1])
                for item in (payload.get("process_identities") or ())
                if isinstance(item, (list, tuple)) and len(item) == 2
            )
            try:
                records[key] = ProcessRecord(**payload)
            except (TypeError, ValueError):
                continue
        return records

    def _save(self, records: dict[str, ProcessRecord]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: asdict(value) for key, value in records.items()}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.registry_path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, indent=2)
            temporary = Path(handle.name)
        os.replace(temporary, self.registry_path)


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except SystemError:
        # Windows can surface access-denied from os.kill(pid, 0) as a
        # SystemError.  This probe is not ownership evidence and must never
        # abort reconciliation.  Treat the PID as not alive for stale-record
        # reconciliation; all termination paths still require a verified
        # command/creation-time topology from _process_snapshot().
        return False
    return True


def write_supervisor_diagnostic(
    project_root: Path,
    *,
    operation: str,
    stage: str,
    exc: BaseException,
    secrets: tuple[str, ...] = (),
) -> Path | None:
    """Append a redacted local lifecycle diagnostic without raising upstream."""

    def redact(value: str) -> str:
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    path = project_root / "logs" / "supervisor_control_diagnostics.jsonl"
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "operation": operation,
        "stage": stage,
        "exception_type": type(exc).__name__,
        "exception_message": redact(str(exc)),
        "traceback": redact("".join(traceback.format_exception(exc))),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return path


def write_supervisor_lifecycle_event(
    project_root: Path,
    *,
    operation: str,
    stage: str,
    component: str,
    details: dict[str, Any],
) -> Path | None:
    """Append safe component lifecycle evidence to the local diagnostic log."""

    path = project_root / "logs" / "supervisor_control_diagnostics.jsonl"
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "operation": operation,
        "stage": stage,
        "component": component,
        "details": details,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return None
    return path


def _subprocess_creation_flags() -> int:
    """Create normal Python children without a visible Windows console."""

    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if _is_windows():
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _is_windows() -> bool:
    return os.name == "nt"


def resolve_supervised_python(executable: str | None = None) -> str:
    """Resolve the console Python used for supervised API/live children.

    Task Scheduler may start the control plane with ``pythonw.exe``.  A child
    launched with ``sys.executable`` would then inherit that windowless
    interpreter and hide startup diagnostics.  On Windows only the sibling
    ``python.exe`` from that same interpreter directory is accepted; no
    machine-specific installation path is assumed.
    """

    selected = executable or sys.executable
    if not _is_windows():
        return selected
    candidate = Path(selected)
    name = candidate.name.casefold()
    if name == "pythonw.exe":
        candidate = candidate.with_name("python.exe")
    elif name != "python.exe":
        raise RuntimeError("active interpreter is not a supported Python executable")
    if not candidate.is_file():
        raise RuntimeError(f"console Python interpreter is unavailable: {candidate}")
    return str(candidate)


def _process_snapshot() -> dict[int, ProcessIdentity]:
    """Read process identity once; never infer ownership from PID alone."""

    if os.name != "nt":
        return {}
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object {$_.Name -in @('python.exe','pythonw.exe')} | "
                "Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | "
                "ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            creationflags=_subprocess_creation_flags(),
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else []
        rows = payload if isinstance(payload, list) else [payload]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    snapshot: dict[int, ProcessIdentity] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row["ProcessId"])
            parent = int(row.get("ParentProcessId") or 0) or None
        except (KeyError, TypeError, ValueError):
            continue
        if pid == os.getpid():
            continue
        snapshot[pid] = ProcessIdentity(
            pid=pid,
            parent_pid=parent,
            creation_time=str(row.get("CreationDate") or "") or None,
            executable=str(row.get("ExecutablePath") or "") or None,
            command_line=str(row.get("CommandLine") or ""),
        )
    return snapshot


def _command_matches(command_line: str, command: list[str] | tuple[str, ...]) -> bool:
    """Exact argument-tail match; no basename fallback."""

    if not command or not command_line:
        return False
    expected = subprocess.list2cmdline([str(part) for part in command[1:]])
    observed = command_line.casefold().replace("/", "\\").replace('"', "")
    for needle in (expected, expected.replace('"', "")):
        needle = needle.casefold().replace("/", "\\")
        offset = observed.find(needle)
        if offset >= 0:
            end = offset + len(needle)
            if end == len(observed) or observed[end] in " \t\"'":
                return True
    return False


def _matching_descendants(
    root_pid: int,
    snapshot: dict[int, ProcessIdentity],
    command: list[str] | tuple[str, ...],
) -> set[int]:
    owned = {root_pid} if root_pid in snapshot else set()
    changed = True
    while changed:
        changed = False
        for identity in snapshot.values():
            if (
                identity.parent_pid in owned
                and identity.pid not in owned
                and _command_matches(identity.command_line, command)
            ):
                owned.add(identity.pid)
                changed = True
    return owned


def _matching_command_roots(
    snapshot: dict[int, ProcessIdentity],
    command: list[str] | tuple[str, ...],
) -> list[int]:
    matches = {
        identity.pid
        for identity in snapshot.values()
        if _command_matches(identity.command_line, command)
    }
    return sorted(
        pid
        for pid in matches
        if snapshot[pid].parent_pid not in matches
    )


def _record_matches_process(record: ProcessRecord) -> bool:
    """Verify PID, command and creation identity for a persisted record."""

    if not record.pid or not record.command:
        return False
    if os.name != "nt":
        return False
    snapshot = _process_snapshot()
    identity = snapshot.get(record.pid)
    if identity is not None and _command_matches(identity.command_line, record.command):
        return bool(
            record.process_create_time
            and identity.creation_time
            and identity.creation_time == record.process_create_time
        )
    return bool(_verified_stored_pids(record, snapshot, record.command))


def record_process_is_alive(record: ProcessRecord) -> bool:
    """Public read-only identity check used by API health reporting."""

    return _record_matches_process(record)


def _verified_owned_pids(
    record: ProcessRecord,
    child: subprocess.Popen[Any] | None,
) -> list[int] | None:
    """Return only verified owned PIDs; ``None`` means fail closed."""

    if child is not None and child.poll() is None and os.name != "nt":
        return [child.pid]
    if record.pid is None:
        if os.name != "nt":
            return [child.pid] if child is not None and child.poll() is None else []
        return None
    if os.name != "nt":
        return None
    snapshot = _process_snapshot()
    identity = snapshot.get(record.pid)
    if identity is not None and _command_matches(identity.command_line, record.command):
        if not (
            record.process_create_time
            and identity.creation_time == record.process_create_time
        ):
            return None
        owned = _matching_descendants(record.pid, snapshot, record.command)
    else:
        owned = _verified_stored_pids(record, snapshot, record.command)
        if not owned:
            return None
    owned.update(_verified_stored_pids(record, snapshot, record.command))
    return sorted(owned, reverse=True) if owned else [record.pid]


def _verified_stored_pids(
    record: ProcessRecord,
    snapshot: dict[int, ProcessIdentity],
    command: list[str] | tuple[str, ...],
) -> set[int]:
    """Verify persisted PID/creation pairs, including an orphaned interpreter child."""

    verified: set[int] = set()
    for pid, creation_time in record.process_identities:
        identity = snapshot.get(pid)
        if identity is None or not _command_matches(identity.command_line, command):
            continue
        if not creation_time or identity.creation_time != creation_time:
            continue
        verified.add(pid)
    return verified


def _terminate_owned_processes(
    pids: list[int],
    child: subprocess.Popen[Any] | None,
) -> None:
    """Terminate only the verified process topology, deepest child first."""

    for pid in pids:
        # On Windows, the ownership snapshot immediately before this call has
        # already verified PID, command, and creation time.  Do not perform a
        # second os.kill(pid, 0) probe here: CPython can surface WinError 87
        # from that Windows probe with a pending native exception, which then
        # turns the following context-manager construction into SystemError.
        # taskkill's non-zero "not found" result is a normal exit race and is
        # resolved by the verified post-termination wait below.
        if os.name != "nt" and not _pid_alive(pid):
            continue
        if os.name == "nt":
            with _suppress_os_error():
                subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    creationflags=_subprocess_creation_flags(),
                )
        else:
            with _suppress_os_error():
                os.kill(pid, signal.SIGTERM)
    if child is not None and child.poll() is None and os.name != "nt":
        with _suppress_os_error():
            child.terminate()


def _wait_for_pids_to_exit(pids: list[int], *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pids_are_alive(pids):
            return True
        time.sleep(0.05)
    return not _pids_are_alive(pids)


def _pids_are_alive(pids: list[int]) -> bool:
    if os.name == "nt":
        snapshot = _process_snapshot()
        return any(pid in snapshot for pid in pids)
    return any(_pid_alive(pid) for pid in pids)


def _pid_matches_command(pid: int, command: tuple[str, ...]) -> bool:
    """Compatibility wrapper for callers that need verified command identity."""

    if os.name != "nt":
        return False
    identity = _process_snapshot().get(pid)
    return identity is not None and _command_matches(identity.command_line, command)


def _find_matching_processes(command: list[str]) -> list[int]:
    """Find independent command roots, collapsing launcher/interpreter trees."""

    if os.name != "nt" or not command:
        return []
    snapshot = _process_snapshot()
    return _matching_command_roots(snapshot, command)


class _suppress_os_error:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        return exc_type is not None and issubclass(exc_type, OSError)
#phase255b
#pad
