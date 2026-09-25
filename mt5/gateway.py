"""Serialized asynchronous gateway around the synchronous MT5 Python API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from config.settings import Settings
from mt5.connection import MT5Connection


@dataclass(frozen=True, slots=True)
class GatewayCallTiming:
    waiting_for_access_ms: float
    operation_ms: float


class MT5GatewayBusyError(RuntimeError):
    """Raised when an opportunistic diagnostic cannot acquire the gateway."""


class MT5Gateway:
    """Keep one verified MT5 session and serialize every API call."""

    def __init__(
        self, settings: Settings, *, module: Any | None = None, logger: logging.Logger | None = None
    ) -> None:
        self._settings = settings
        self._connection = MT5Connection(settings, module=module, logger=logger)
        self._logger = logger or logging.getLogger(__name__)
        self._lock = asyncio.Lock()
        self._waiting_calls = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def api(self) -> Any:
        return self._connection.api

    async def connect(self) -> Any:
        await self._acquire()
        try:
            if not self._connected:
                await asyncio.to_thread(self._connection.connect)
                self._connected = True
            return self.api
        finally:
            self._lock.release()

    async def call(self, operation: Callable[[Any], Any]) -> Any:
        await self._acquire()
        try:
            if not self._connected:
                raise RuntimeError("MT5 gateway is not connected")
            return await asyncio.to_thread(operation, self.api)
        finally:
            self._lock.release()

    async def _acquire(self) -> None:
        """Track queued gateway work without changing the lock's FIFO behavior."""

        self._waiting_calls += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiting_calls -= 1

    async def call_if_idle(self, operation: Callable[[Any], Any]) -> Any:
        """Run one read only when the serialized gateway is currently idle.

        Diagnostics must not queue ahead of normal polling work. The lock is
        acquired through the same ownership boundary as every regular MT5 call.
        """

        if self._lock.locked() or self._waiting_calls:
            raise MT5GatewayBusyError("MT5 gateway is busy")
        await self._acquire()
        try:
            if not self._connected:
                raise RuntimeError("MT5 gateway is not connected")
            return await asyncio.to_thread(operation, self.api)
        finally:
            self._lock.release()

    async def call_timed(
        self,
        operation: Callable[[Any], Any],
        *,
        on_stage: Callable[[str, float], None] | None = None,
    ) -> tuple[Any, GatewayCallTiming]:
        """Run a serialized call and separately report lock wait and call time."""

        waiting_started = perf_counter()
        if on_stage is not None:
            try:
                on_stage("WAITING_FOR_MT5_ACCESS", waiting_started)
            except Exception:
                self._log_timing_callback_failure()
        await self._acquire()
        acquired = perf_counter()
        try:
            if not self._connected:
                raise RuntimeError("MT5 gateway is not connected")
            if on_stage is not None:
                try:
                    on_stage("MT5_GATEWAY_OPERATION", acquired)
                except Exception:
                    self._log_timing_callback_failure()
            operation_started = perf_counter()
            try:
                result = await asyncio.to_thread(operation, self.api)
            finally:
                completed = perf_counter()
                if on_stage is not None:
                    try:
                        on_stage("MT5_GATEWAY_OPERATION_COMPLETED", completed)
                    except Exception:
                        self._log_timing_callback_failure()
            return result, GatewayCallTiming(
                waiting_for_access_ms=max(0.0, (acquired - waiting_started) * 1_000),
                operation_ms=max(0.0, (completed - operation_started) * 1_000),
            )
        finally:
            self._lock.release()

    def _log_timing_callback_failure(self) -> None:
        """Log only a fixed diagnostic message; never include exception data."""

        with contextlib.suppress(Exception):
            self._logger.debug("MT5 timing callback failed")

    async def shutdown(self) -> None:
        await self._acquire()
        try:
            if self._connected:
                await asyncio.to_thread(self._connection.shutdown)
                self._connected = False
        finally:
            self._lock.release()
