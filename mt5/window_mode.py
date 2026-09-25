"""Opt-in, fail-closed hiding of the configured MT5 top-level window on Windows."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    executable_path: str
    creation_time: int


class WindowAdapter(Protocol):
    def process_identity(self, pid: int) -> ProcessIdentity | None: ...

    def top_level_windows(self, pid: int) -> tuple[int, ...]: ...

    def window_owner_pid(self, hwnd: int) -> int | None: ...

    def is_window_visible(self, hwnd: int) -> bool: ...

    def hide_window(self, hwnd: int) -> None: ...


class WindowsWindowAdapter:
    """Small native adapter; it never inspects window titles or process command lines."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SW_HIDE = 0

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows window APIs are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
        self._kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        self._kernel32.GetProcessTimes.restype = ctypes.c_bool
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_bool
        self._user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._user32.EnumWindows.restype = ctypes.c_bool
        self._user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self._user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        self._user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self._user32.IsWindowVisible.restype = ctypes.c_bool
        self._user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._user32.ShowWindow.restype = ctypes.c_bool

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        handle = self._kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            capacity = 32768
            buffer = ctypes.create_unicode_buffer(capacity)
            length = ctypes.c_ulong(capacity)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(length)
            ):
                return None
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel_time = ctypes.c_ulonglong()
            user_time = ctypes.c_ulonglong()
            if not self._kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return ProcessIdentity(pid, buffer.value, creation.value)
        finally:
            self._kernel32.CloseHandle(handle)

    def top_level_windows(self, pid: int) -> tuple[int, ...]:
        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def collect(hwnd: int, _lparam: int) -> bool:
            owner = ctypes.c_ulong()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                handles.append(int(hwnd))
            return True

        callback = callback_type(collect)
        if not self._user32.EnumWindows(ctypes.cast(callback, ctypes.c_void_p), 0):
            raise OSError("EnumWindows failed")
        return tuple(handles)

    def window_owner_pid(self, hwnd: int) -> int | None:
        owner = ctypes.c_ulong()
        if not self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)):
            return None
        return int(owner.value)

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def hide_window(self, hwnd: int) -> None:
        self._user32.ShowWindow(hwnd, self.SW_HIDE)


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))


class MT5BackgroundWindowController:
    """Bounded initial hide plus low-frequency recovery for later MT5 windows/PIDs."""

    VALID_STATES = {
        "DISABLED",
        "WAITING_FOR_PROCESS",
        "WAITING_FOR_WINDOW",
        "HIDDEN",
        "VISIBLE",
        "UNAVAILABLE",
        "ERROR",
    }

    def __init__(
        self,
        *,
        enabled: bool,
        configured_path: str | None,
        process_probe: Callable[[], tuple[Any, ...]],
        logger: logging.Logger | None = None,
        adapter: WindowAdapter | None = None,
        windows: bool | None = None,
        retry_seconds: float = 3.0,
        retry_interval: float = 0.25,
        monitor_interval: float = 2.0,
        monotonic: Callable[[], float] | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        from time import monotonic as monotonic_clock

        self.enabled = enabled
        self.configured_path = configured_path
        self._process_probe = process_probe
        self._logger = logger or logging.getLogger(__name__)
        self._windows = os.name == "nt" if windows is None else windows
        self._adapter = adapter
        self._retry_seconds = max(0.0, retry_seconds)
        self._retry_interval = max(0.01, retry_interval)
        self._monitor_interval = max(0.1, monitor_interval)
        self._monotonic = monotonic or monotonic_clock
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = "DISABLED" if not enabled else "WAITING_FOR_PROCESS"
        self._pid: int | None = None
        self._last_exception_type: str | None = None

        if enabled and self._windows and adapter is None:
            try:
                self._adapter = WindowsWindowAdapter()
            except Exception as exc:
                self._set_state("UNAVAILABLE", exception_type=type(exc).__name__)
        elif enabled and not self._windows:
            self._set_state("UNAVAILABLE")

        # Injectable Event.wait avoids real waits in tests while retaining a stoppable worker.
        self._wait = wait or self._stop_event.wait

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "state": self._state,
                "pid": self._pid,
                "last_error_type": self._last_exception_type,
            }

    def start(self, preferred_pid: int | None = None) -> None:
        if not self.enabled:
            return
        if not self._windows or self._adapter is None:
            self._set_state("UNAVAILABLE")
            return
        deadline = self._monotonic() + self._retry_seconds
        while True:
            state = self._observe(preferred_pid)
            if state in {"HIDDEN", "VISIBLE", "ERROR", "UNAVAILABLE"}:
                break
            if self._monotonic() >= deadline:
                break
            should_stop = self._wait(
                min(self._retry_interval, max(0.0, deadline - self._monotonic()))
            )
            if should_stop:
                break
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._monitor,
                    name="mt5-background-window-monitor",
                    daemon=True,
                )
                self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._monitor_interval + 0.5))
        with self._lock:
            self._thread = None

    def observe_once(self, preferred_pid: int | None = None) -> str:
        """One deterministic monitoring pass, also useful to exercise recovery in tests."""
        return self._observe(preferred_pid)

    def _monitor(self) -> None:
        while not self._wait(self._monitor_interval):
            try:
                self._observe()
            except Exception as exc:  # diagnostics/UI behavior must not affect MT5 readiness
                self._set_state("ERROR", exception_type=type(exc).__name__)

    def _observe(self, preferred_pid: int | None = None) -> str:
        if not self.enabled:
            self._set_state("DISABLED")
            return self.state
        if not self._windows or self._adapter is None:
            self._set_state("UNAVAILABLE")
            return self.state
        if not self.configured_path:
            self._set_state("ERROR")
            return self.state
        try:
            candidates = tuple(self._process_probe())
            matching: list[ProcessIdentity] = []
            for process in candidates:
                identity = self._adapter.process_identity(process.pid)
                if identity is None:
                    self._set_state("ERROR", pid=process.pid)
                    return self.state
                if _normalized_path(identity.executable_path) == _normalized_path(
                    self.configured_path
                ):
                    matching.append(identity)
            if len(matching) > 1:
                self._set_state("ERROR")
                return self.state
            identity = matching[0] if matching else None
            if identity is None and preferred_pid is not None:
                # A just-launched PID must independently match the configured image.
                identity = self._adapter.process_identity(preferred_pid)
                if identity is None:
                    self._set_state("WAITING_FOR_PROCESS")
                    return self.state
                if _normalized_path(identity.executable_path) != _normalized_path(
                    self.configured_path
                ):
                    self._set_state("WAITING_FOR_PROCESS")
                    return self.state
            if identity is None:
                self._set_state("WAITING_FOR_PROCESS")
                return self.state
            return self._hide_verified(identity)
        except Exception as exc:
            self._set_state("ERROR", exception_type=type(exc).__name__)
            return self.state

    def _hide_verified(self, identity: ProcessIdentity) -> str:
        assert self._adapter is not None
        windows = self._adapter.top_level_windows(identity.pid)
        if not windows:
            self._set_state("WAITING_FOR_WINDOW", pid=identity.pid)
            return self.state
        visible_found = False
        for hwnd in windows:
            current = self._adapter.process_identity(identity.pid)
            if current != identity or self._adapter.window_owner_pid(hwnd) != identity.pid:
                self._set_state("ERROR", pid=identity.pid)
                return self.state
            if not self._adapter.is_window_visible(hwnd):
                continue
            visible_found = True
            self._adapter.hide_window(hwnd)
            current = self._adapter.process_identity(identity.pid)
            if current != identity or self._adapter.window_owner_pid(hwnd) != identity.pid:
                self._set_state("ERROR", pid=identity.pid)
                return self.state
            if self._adapter.is_window_visible(hwnd):
                self._set_state("ERROR", pid=identity.pid)
                return self.state
        if self._adapter.process_identity(identity.pid) != identity:
            self._set_state("ERROR", pid=identity.pid)
            return self.state
        self._set_state("HIDDEN" if visible_found or windows else "VISIBLE", pid=identity.pid)
        return self.state

    def _set_state(
        self, state: str, *, pid: int | None = None, exception_type: str | None = None
    ) -> None:
        if state not in self.VALID_STATES:
            state = "ERROR"
        with self._lock:
            changed = state != self._state or pid != self._pid
            self._state = state
            self._pid = pid
            self._last_exception_type = exception_type
        if changed:
            # Do not include process paths, window titles, exception messages, or credentials.
            self._logger.info("MT5 background window state: %s", state)
