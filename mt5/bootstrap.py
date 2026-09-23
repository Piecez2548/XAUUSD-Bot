"""Fail-closed MT5 terminal launch and read-only readiness verification."""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from config.settings import Settings
from models.market import Timeframe
from mt5.connection import MT5Connection, MT5ConnectionError
from mt5.market_data import MarketDataError, read_completed_candles
from mt5.symbols import (
    SymbolDiscoveryError,
    discover_symbol,
    read_symbol_specification,
    read_tick,
)


class MT5BootstrapError(RuntimeError):
    """Raised when terminal launch or read-only readiness verification fails."""


@dataclass(frozen=True, slots=True)
class TerminalProcess:
    pid: int
    image_name: str = "terminal64.exe"


@dataclass(frozen=True, slots=True)
class MT5Verification:
    account_login: int | None
    account_server: str | None
    symbol: str
    candle_count: int


@dataclass(frozen=True, slots=True)
class MT5StartupResult:
    ready: bool
    launch_state: str
    pid: int | None
    checks: dict[str, str]
    reason: str | None = None
    verification: MT5Verification | None = None


def _default_process_probe() -> tuple[TerminalProcess, ...]:
    """List terminal64.exe processes without terminating or adopting them."""

    if os.name != "nt":
        return ()
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            creationflags=_background_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    processes: list[TerminalProcess] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2 or row[0].casefold() != "terminal64.exe":
            continue
        try:
            processes.append(TerminalProcess(pid=int(row[1])))
        except ValueError:
            continue
    return tuple(processes)


def _background_creation_flags() -> int:
    """Hide only background inspection helpers on Windows."""

    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if os.name == "nt":
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def verify_mt5_readiness(settings: Settings, database: Any) -> MT5Verification:
    """Connect once and verify terminal, account, symbol, candles, and database."""

    connection = MT5Connection(settings)
    try:
        api = connection.connect().api
        terminal = api.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            raise MT5BootstrapError("terminal is not connected")
        account = api.account_info()
        if account is None:
            raise MT5BootstrapError("expected account is unavailable")
        account_login = getattr(account, "login", None)
        if settings.mt5_login is not None and (
            account_login is None or int(account_login) != settings.mt5_login
        ):
            raise MT5BootstrapError("expected account validation failed")
        account_server = getattr(account, "server", None)
        if settings.mt5_server and account_server != settings.mt5_server:
            raise MT5BootstrapError("expected broker server validation failed")
        symbol, _candidates = discover_symbol(api, settings.trading_symbol)
        read_symbol_specification(api, symbol)
        read_tick(api, symbol)
        candles = read_completed_candles(
            api,
            symbol,
            Timeframe.M5,
            2,
            minimum_ratio=0.8,
        )
        if len(candles) < 2:
            raise MT5BootstrapError("market data returned fewer than two closed M5 candles")
        if not database.healthcheck():
            raise MT5BootstrapError("database health check failed")
        return MT5Verification(
            account_login=int(account_login) if account_login is not None else None,
            account_server=str(account_server) if account_server is not None else None,
            symbol=symbol,
            candle_count=len(candles),
        )
    except MT5BootstrapError:
        raise
    except (MT5ConnectionError, MarketDataError, SymbolDiscoveryError) as exc:
        raise MT5BootstrapError(str(exc)) from exc
    finally:
        connection.shutdown()


class MT5AutoLauncher:
    """Ensure one terminal is available, then verify a read-only MT5 session."""

    def __init__(
        self,
        settings: Settings,
        *,
        logger: logging.Logger | None = None,
        process_probe: Callable[[], Sequence[TerminalProcess]] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        verifier: Callable[[Settings, Any], MT5Verification] = verify_mt5_readiness,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.settings = settings
        self.logger = logger or logging.getLogger(__name__)
        self._process_probe = process_probe or _default_process_probe
        self._popen_factory = popen_factory
        self._verifier = verifier
        self._sleep = sleep
        self._monotonic = monotonic_clock
        self._launched_process: Any | None = None

    async def ensure_ready(self, database: Any) -> MT5StartupResult:
        deadline = self._monotonic() + self.settings.mt5_startup_timeout_seconds
        try:
            launch_state, pid = await self._ensure_terminal(deadline)
        except MT5BootstrapError as exc:
            return MT5StartupResult(
                ready=False,
                launch_state="FAILED",
                pid=None,
                checks={"mt5_process": "FAILED"},
                reason=str(exc),
            )
        try:
            verification = await self._verify_until_ready(database, deadline)
        except Exception as exc:
            reason = self._safe_reason(exc)
            self.logger.warning("MT5 readiness verification failed (%s)", type(exc).__name__)
            return MT5StartupResult(
                ready=False,
                launch_state=launch_state,
                pid=pid,
                checks={
                    "mt5_process": launch_state,
                    "terminal": "FAILED",
                    "account": "FAILED",
                    "symbol": "FAILED",
                    "market_data": "FAILED",
                    "database": "PENDING",
                },
                reason=reason,
            )
        return MT5StartupResult(
            ready=True,
            launch_state=launch_state,
            pid=pid,
            checks={
                "mt5_process": launch_state,
                "terminal": "CONNECTED",
                "account": "VERIFIED",
                "symbol": "AVAILABLE",
                "market_data": "READY",
                "database": "CONNECTED",
            },
            verification=verification,
        )

    async def _ensure_terminal(self, deadline: float) -> tuple[str, int | None]:
        existing = tuple(self._process_probe())
        if existing:
            return "REUSED", existing[0].pid
        if self._launched_process is not None and self._process_alive(self._launched_process):
            pid = getattr(self._launched_process, "pid", None)
            await self._wait_for_terminal(pid, deadline)
            return "STARTED", pid
        if not self.settings.mt5_auto_launch:
            raise MT5BootstrapError("MT5 terminal is not running and MT5_AUTO_LAUNCH=false")
        path_value = self.settings.mt5_terminal_path
        if not path_value:
            raise MT5BootstrapError("MT5_TERMINAL_PATH is not configured")
        path = Path(path_value).expanduser()
        if not path.is_file():
            raise MT5BootstrapError("configured MT5_TERMINAL_PATH does not exist")
        try:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self._launched_process = self._popen_factory(
                [str(path)],
                cwd=str(path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=flags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MT5BootstrapError("MT5 terminal launch failed") from exc
        pid = getattr(self._launched_process, "pid", None)
        await self._wait_for_terminal(pid, deadline)
        return "STARTED", pid

    async def _wait_for_terminal(self, pid: int | None, deadline: float) -> None:
        while self._monotonic() < deadline:
            processes = tuple(self._process_probe())
            if processes and (pid is None or any(item.pid == pid for item in processes)):
                return
            if self._launched_process is not None:
                exit_code = self._launched_process.poll()
                if exit_code is not None:
                    raise MT5BootstrapError(
                        f"MT5 terminal exited during startup (exit_code={exit_code})"
                    )
            await self._sleep(0.25)
        raise MT5BootstrapError("MT5 terminal startup timed out")

    async def _verify_until_ready(self, database: Any, deadline: float) -> MT5Verification:
        last_error: Exception | None = None
        while True:
            try:
                return await asyncio.to_thread(self._verifier, self.settings, database)
            except Exception as exc:
                last_error = exc
                reason = str(exc).casefold()
                retryable = any(
                    marker in reason
                    for marker in (
                        "not connected",
                        "initialization failed",
                        "terminal information",
                        "account information",
                    )
                )
                if not retryable or self._monotonic() >= deadline:
                    raise
                await self._sleep(0.25)
        raise AssertionError(last_error)

    @staticmethod
    def _process_alive(process: Any) -> bool:
        try:
            return process.poll() is None
        except (AttributeError, OSError):
            return False

    def _safe_reason(self, exc: Exception) -> str:
        reason = str(exc) or type(exc).__name__
        for secret in (self.settings.mt5_password,):
            if secret:
                reason = reason.replace(secret, "[REDACTED]")
        return reason
