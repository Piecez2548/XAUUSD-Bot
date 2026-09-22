"""Lifecycle management for a read-only MetaTrader 5 session."""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

from config.settings import Settings


class MT5ConnectionError(RuntimeError):
    """Raised when an MT5 session cannot be initialized or verified."""


class MT5Connection:
    """Own and verify one MT5 connection, with guaranteed clean shutdown."""

    def __init__(
        self,
        settings: Settings,
        module: ModuleType | Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._module = module
        self._logger = logger or logging.getLogger(__name__)
        self._connected = False

    @property
    def api(self) -> Any:
        """Return the MT5 API only after the read-only session is verified."""

        if not self._connected or self._module is None:
            raise MT5ConnectionError("MT5 is not connected")
        return self._module

    def connect(self) -> MT5Connection:
        if self._connected:
            return self
        if self._module is None:
            try:
                self._module = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise MT5ConnectionError(
                    "MetaTrader5 package is not installed; install requirements in a "
                    "Python 3.11+ environment"
                ) from exc

        kwargs: dict[str, object] = {}
        if self._settings.mt5_terminal_path:
            kwargs["path"] = self._settings.mt5_terminal_path
        if self._settings.mt5_login is not None:
            kwargs.update(
                login=self._settings.mt5_login,
                server=self._settings.mt5_server,
                password=self._settings.mt5_password,
            )

        self._logger.info(
            "Initializing MT5 terminal (explicit_credentials=%s, custom_path=%s)",
            self._settings.mt5_login is not None,
            self._settings.mt5_terminal_path is not None,
        )
        if not self._module.initialize(**kwargs):
            error = self._last_error()
            self._module.shutdown()
            raise MT5ConnectionError(f"MT5 initialization failed: {error}")

        try:
            terminal = self._module.terminal_info()
            if terminal is None:
                raise MT5ConnectionError(
                    f"MT5 terminal information is unavailable: {self._last_error()}"
                )
            if not bool(getattr(terminal, "connected", False)):
                raise MT5ConnectionError(
                    "MT5 terminal is initialized but not connected to a broker"
                )
            if self._module.account_info() is None:
                raise MT5ConnectionError(
                    f"MT5 account information is unavailable: {self._last_error()}"
                )
        except Exception:
            self._module.shutdown()
            raise

        self._connected = True
        self._logger.info("MT5 terminal connection verified")
        return self

    def shutdown(self) -> None:
        if self._module is not None and self._connected:
            self._module.shutdown()
            self._connected = False
            self._logger.info("MT5 terminal shutdown complete")

    def _last_error(self) -> str:
        if self._module is None:
            return "module unavailable"
        try:
            return repr(self._module.last_error())
        except Exception:
            return "error details unavailable"

    def __enter__(self) -> MT5Connection:
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.shutdown()
