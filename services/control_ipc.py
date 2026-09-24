"""Local-only, authenticated IPC for the persistent operator Control process.

The FastAPI process is intentionally not allowed to own or supervise API/Live
children.  On Windows it sends a small, fixed command vocabulary through a
named pipe to the persistent Control process, which invokes the existing
canonical lifecycle handlers.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any
from uuid import UUID

IPC_VERSION = "phase33-control-ipc-v1"
CONTROL_COMMANDS = frozenset(
    {"status", "start", "stop", "restart", "demo_status", "demo_on", "demo_off"}
)
MUTATING_CONTROL_COMMANDS = frozenset({"start", "stop", "restart", "demo_on", "demo_off"})


class ControlIpcError(RuntimeError):
    """Raised when the local Control IPC cannot be reached or validates poorly."""


def pipe_address(project_root: Path) -> str:
    """Return a deterministic per-installation Windows named-pipe address."""

    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\xauusd-control-{digest}"


def ipc_authkey(project_root: Path) -> bytes:
    """Derive the local IPC handshake key from the installation identity.

    This key is never sent to the browser and is only used inside the local
    named-pipe handshake.  The named pipe remains local-only; commands are
    separately validated against the fixed protocol below.
    """

    return hashlib.sha256(
        f"{IPC_VERSION}|{project_root.resolve()}".encode()
    ).digest()


def validate_ipc_request(message: object) -> dict[str, str]:
    """Validate the exact IPC request shape and return a normalized copy."""

    if not isinstance(message, dict):
        raise ControlIpcError("IPC request must be an object")
    allowed = {"operation_id", "command", "actor"}
    if any(key not in allowed for key in message):
        raise ControlIpcError("IPC request contains an unknown field")
    operation_id = message.get("operation_id")
    command = message.get("command")
    actor = message.get("actor", "web")
    if not isinstance(operation_id, str) or not isinstance(command, str):
        raise ControlIpcError("IPC request fields have invalid types")
    try:
        UUID(operation_id)
    except (ValueError, AttributeError):
        raise ControlIpcError("IPC operation_id must be a UUID") from None
    if command not in CONTROL_COMMANDS:
        raise ControlIpcError("IPC command is not allowlisted")
    if not isinstance(actor, str) or not actor or len(actor) > 128:
        raise ControlIpcError("IPC actor is invalid")
    return {"operation_id": operation_id, "command": command, "actor": actor}


ControlHandler = Callable[[dict[str, str]], Awaitable[dict[str, Any]]]


class ControlIpcServer:
    """Serve validated requests from a local named pipe into the Control loop."""

    def __init__(
        self,
        project_root: Path,
        loop,
        handler: ControlHandler,
        *,
        request_timeout_seconds: float = 120.0,
    ) -> None:
        self.project_root = project_root
        self.loop = loop
        self.handler = handler
        self.request_timeout_seconds = request_timeout_seconds
        self.address = pipe_address(project_root)
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if os.name != "nt":
            raise ControlIpcError("Control named-pipe IPC is Windows-only")
        if self._thread is not None:
            return
        try:
            self._listener = Listener(
                self.address, family="AF_PIPE", authkey=ipc_authkey(self.project_root)
            )
        except OSError as exc:
            raise ControlIpcError("Control named-pipe listener could not start") from exc
        self._thread = threading.Thread(
            target=self._serve,
            name="xauusd-control-ipc",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            with suppress(OSError):
                listener.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                if not self._stop.is_set():
                    continue
                return
            try:
                message = connection.recv()
                request = validate_ipc_request(message)
                future = asyncio.run_coroutine_threadsafe(
                    self.handler(request), self.loop
                )
                response = future.result(timeout=self.request_timeout_seconds)
                connection.send(response)
            except Exception as exc:
                with suppress(OSError, EOFError):
                    connection.send(
                        {
                            "ok": False,
                            "error": "Control IPC request failed",
                            "error_type": type(exc).__name__,
                        }
                    )
            finally:
                with suppress(OSError):
                    connection.close()


class ControlIpcClient:
    """Synchronous FastAPI-side client for the local Control named pipe."""

    def __init__(self, project_root: Path, *, timeout_seconds: float = 125.0) -> None:
        self.project_root = project_root
        self.timeout_seconds = timeout_seconds

    def request(self, command: str, operation_id: str, *, actor: str = "web") -> dict[str, Any]:
        request = validate_ipc_request(
            {"command": command, "operation_id": operation_id, "actor": actor}
        )
        if os.name != "nt":
            raise ControlIpcError("Control named-pipe IPC is Windows-only")
        try:
            connection = Client(
                pipe_address(self.project_root),
                family="AF_PIPE",
                authkey=ipc_authkey(self.project_root),
            )
        except (OSError, EOFError) as exc:
            raise ControlIpcError("Control process is unavailable") from exc
        try:
            connection.send(request)
            if not connection.poll(self.timeout_seconds):
                raise ControlIpcError("Control IPC response timed out")
            response = connection.recv()
        except ControlIpcError:
            raise
        except (OSError, EOFError) as exc:
            raise ControlIpcError("Control IPC response was unavailable") from exc
        finally:
            with suppress(OSError):
                connection.close()
        if not isinstance(response, dict) or response.get("operation_id") != operation_id:
            raise ControlIpcError("Control IPC response failed validation")
        return dict(response)


__all__ = [
    "CONTROL_COMMANDS",
    "ControlIpcClient",
    "ControlIpcError",
    "ControlIpcServer",
    "IPC_VERSION",
    "MUTATING_CONTROL_COMMANDS",
    "ipc_authkey",
    "pipe_address",
    "validate_ipc_request",
]
