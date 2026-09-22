"""Conservative local process supervision for the read-only observatory."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
    restart_count: int = 0
    command: tuple[str, ...] = ()
    last_error: str | None = None


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
    """Own API/live child processes and persist truthful lifecycle state."""

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
        self._restart_times: dict[str, list[datetime]] = {}
        self._mutex = RLock()

    def start_component(self, component: str, command: list[str]) -> ProcessRecord:
        with self._mutex, SupervisorOperationLock(self.lock_path):
            records = self._load()
            existing = records.get(component)
            child = self._children.get(component)
            matches = _find_matching_processes(command)
            if len(matches) > 1:
                # Do not adopt one arbitrarily: two live processes can both
                # poll history and generate duplicate shadow work.  Leave
                # both untouched and require an explicit operator cleanup.
                record = existing or ProcessRecord(
                    component=component,
                    pid=None,
                    state="DEGRADED",
                    started_at=None,
                    last_heartbeat=None,
                    exit_code=None,
                )
                record.state = "DEGRADED"
                record.last_error = "multiple matching processes detected"
                records[component] = record
                self._save(records)
                return record
            if child is not None and child.poll() is None:
                return self._record(records, component, child.pid, "RUNNING", command)
            if (
                existing
                and existing.state in {"RUNNING", "STARTING"}
                and existing.pid
                and _pid_alive(existing.pid)
            ):
                if _pid_matches_command(existing.pid, existing.command):
                    return existing
                existing.state = "DEGRADED"
                existing.last_error = "live PID identity could not be verified"
                records[component] = existing
                self._save(records)
                return existing
            # A prior supervisor instance may have lost its registry update
            # while its child is still alive.  Before spawning, conservatively
            # adopt one exact command match found in the OS process table.
            adopted_pid = matches[0] if matches else None
            if adopted_pid is not None:
                started = existing.started_at if existing else datetime.now(UTC).isoformat()
                record = ProcessRecord(
                    component=component,
                    pid=adopted_pid,
                    state="RUNNING",
                    started_at=started,
                    last_heartbeat=datetime.now(UTC).isoformat(),
                    exit_code=None,
                    restart_count=existing.restart_count if existing else 0,
                    command=tuple(command),
                )
                records[component] = record
                self._save(records)
                return record
            started = datetime.now(UTC)
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            child = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=flags,
            )
            self._children[component] = child
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
            )
            records[component] = record
            self._save(records)
            return record

    def stop_component(self, component: str) -> ProcessRecord:
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
            child = self._children.get(component)
            if child is not None and child.poll() is None:
                record.state = "STOPPING"
                self._save(records | {component: record})
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                record.exit_code = child.returncode
                self._children.pop(component, None)
            elif record.pid and _pid_alive(record.pid):
                record.state = "DEGRADED"
                record.last_error = "process identity is unverified; not terminated"
                records[component] = record
                self._save(records)
                return record
            record.state = "STOPPED"
            record.pid = None
            record.last_heartbeat = datetime.now(UTC).isoformat()
            records[component] = record
            self._save(records)
            return record

    def restart_component(self, component: str, command: list[str]) -> ProcessRecord:
        self.stop_component(component)
        return self.start_component(component, command)

    def monitor_once(self, commands: dict[str, list[str]]) -> tuple[ProcessRecord, ...]:
        with self._mutex:
            records = self._load()
            changed: list[ProcessRecord] = []
            now = datetime.now(UTC)
            for component, child in tuple(self._children.items()):
                code = child.poll()
                if code is None:
                    record = records[component]
                    record.last_heartbeat = now.isoformat()
                    records[component] = record
                    continue
                record = records[component]
                record.exit_code = code
                record.pid = None
                record.state = "CRASHED"
                history = [
                    item
                    for item in self._restart_times.get(component, [])
                    if now - item <= self.restart_window
                ]
                self._restart_times[component] = history
                self._children.pop(component, None)
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
            for component, child in self._children.items():
                record = records.get(component)
                if record is not None and child.poll() is None:
                    record.state = "RUNNING"
                    record.last_heartbeat = datetime.now(UTC).isoformat()
            for component, record in records.items():
                if (
                    component not in self._children
                    and record.pid
                    and record.state in {"RUNNING", "STARTING"}
                    and not _pid_matches_command(record.pid, record.command)
                ):
                    record.state = "DEGRADED"
                    record.last_error = "live PID identity could not be verified"
            self._save(records)
            return records

    def _record(
        self,
        records: dict[str, ProcessRecord],
        component: str,
        pid: int | None,
        state: str,
        command: list[str],
    ) -> ProcessRecord:
        record = records[component]
        record.pid = pid
        record.state = state
        record.command = tuple(command)
        record.last_heartbeat = datetime.now(UTC).isoformat()
        self._save(records)
        return record

    def _load(self) -> dict[str, ProcessRecord]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {key: ProcessRecord(**value) for key, value in data.items()}

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
    return True


def _pid_matches_command(pid: int, command: tuple[str, ...]) -> bool:
    """Verify a persisted PID conservatively; never kill an unverified process."""

    if not command:
        return False
    # A child owned by this supervisor is identity-safe by construction.
    # Persisted processes require a Windows command-line check after restart.
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command_line = result.stdout.casefold()
    return all(part.casefold() in command_line for part in command[1:])


def _find_matching_processes(command: list[str]) -> list[int]:
    """Find an existing exact child command without terminating anything."""

    if os.name != "nt" or not command:
        return []
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else []
        rows = payload if isinstance(payload, list) else [payload]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    exact: list[int] = []
    fallback: list[int] = []
    executable = command[0].casefold()
    for row in rows:
        try:
            pid = int(row["ProcessId"])
            command_line = str(row.get("CommandLine") or "").casefold()
        except (KeyError, TypeError, ValueError):
            continue
        if pid == os.getpid() or not all(part.casefold() in command_line for part in command[1:]):
            continue
        if executable in command_line:
            exact.append(pid)
        else:
            fallback.append(pid)
    return sorted(exact or fallback)


class _suppress_os_error:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        return exc_type is not None and issubclass(exc_type, OSError)
