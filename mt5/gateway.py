"""Serialized asynchronous gateway around the synchronous MT5 Python API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from config.settings import Settings
from mt5.connection import MT5Connection


class MT5Gateway:
    """Keep one verified MT5 session and serialize every API call."""

    def __init__(
        self, settings: Settings, *, module: Any | None = None, logger: logging.Logger | None = None
    ) -> None:
        self._settings = settings
        self._connection = MT5Connection(settings, module=module, logger=logger)
        self._logger = logger or logging.getLogger(__name__)
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def api(self) -> Any:
        return self._connection.api

    async def connect(self) -> Any:
        async with self._lock:
            if not self._connected:
                await asyncio.to_thread(self._connection.connect)
                self._connected = True
            return self.api

    async def call(self, operation: Callable[[Any], Any]) -> Any:
        async with self._lock:
            if not self._connected:
                raise RuntimeError("MT5 gateway is not connected")
            return await asyncio.to_thread(operation, self.api)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._connected:
                await asyncio.to_thread(self._connection.shutdown)
                self._connected = False
