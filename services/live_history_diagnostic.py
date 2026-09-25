"""Bounded, local-only diagnostics served by the existing Live MT5 gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any
from uuid import UUID

from mt5.gateway import MT5GatewayBusyError
from services.control_ipc import ipc_authkey

MAX_HISTORY_RANGE = timedelta(hours=4)
MAX_RETURNED_BARS = 20
MAX_DIAGNOSTIC_MESSAGE_BYTES = 8_192
LIVE_HISTORY_DIAGNOSTIC_TIMEOUT_SECONDS = 20.0
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class HistoryDiagnosticError(ValueError):
    """A sanitized, stable failure code for the local diagnostic protocol."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HistoryDiagnosticRequest:
    request_id: str
    symbol: str
    timeframe: str
    start: datetime
    end: datetime


def live_history_pipe_address(project_root: Path) -> str:
    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\xauusd-live-history-{digest}"


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise HistoryDiagnosticError("INVALID_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise HistoryDiagnosticError("INVALID_TIMESTAMP") from None
    offset = parsed.utcoffset()
    if offset is None:
        raise HistoryDiagnosticError("INVALID_TIMESTAMP")
    if offset != timedelta(0):
        raise HistoryDiagnosticError("TIMESTAMP_MUST_BE_UTC")
    return parsed.astimezone(UTC)


def validate_history_diagnostic_request(
    payload: object,
    *,
    now: datetime | None = None,
) -> HistoryDiagnosticRequest:
    if not isinstance(payload, dict) or set(payload) != {
        "request_id", "symbol", "timeframe", "start", "end"
    }:
        raise HistoryDiagnosticError("INVALID_REQUEST")
    request_id = payload.get("request_id")
    symbol = payload.get("symbol")
    timeframe = payload.get("timeframe")
    if not isinstance(request_id, str) or not isinstance(symbol, str):
        raise HistoryDiagnosticError("INVALID_REQUEST")
    try:
        UUID(request_id)
    except (ValueError, AttributeError):
        raise HistoryDiagnosticError("INVALID_REQUEST_ID") from None
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise HistoryDiagnosticError("INVALID_SYMBOL")
    if timeframe != "M15":
        raise HistoryDiagnosticError("UNSUPPORTED_TIMEFRAME")
    start = _utc_timestamp(payload.get("start"))
    end = _utc_timestamp(payload.get("end"))
    if end <= start:
        raise HistoryDiagnosticError("INVALID_RANGE")
    if end - start > MAX_HISTORY_RANGE:
        raise HistoryDiagnosticError("RANGE_EXCEEDS_LIMIT")
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise HistoryDiagnosticError("INVALID_CLOCK")
    if end > current.astimezone(UTC):
        raise HistoryDiagnosticError("FUTURE_RANGE_NOT_ALLOWED")
    expected_count = int((end - start) / timedelta(minutes=15)) + 1
    if expected_count > MAX_RETURNED_BARS:
        raise HistoryDiagnosticError("BAR_LIMIT_EXCEEDED")
    return HistoryDiagnosticRequest(request_id, symbol, timeframe, start, end)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _rate_open_timestamp(row: Any) -> datetime:
    try:
        raw_time = row["time"]
    except (KeyError, IndexError, TypeError):
        raw_time = getattr(row, "time", None)
    if isinstance(raw_time, bool) or raw_time is None:
        raise HistoryDiagnosticError("MALFORMED_BROKER_RESULT")
    try:
        return datetime.fromtimestamp(int(raw_time), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        raise HistoryDiagnosticError("MALFORMED_BROKER_RESULT") from None


def _session_metadata(api: Any, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    session_reader = getattr(api, "symbol_info_session_trade", None)
    if not callable(session_reader):
        return {"available": False, "sessions": []}
    days: list[date] = []
    cursor = start.date()
    while cursor <= end.date() and len(days) < 2:
        days.append(cursor)
        cursor += timedelta(days=1)
    sessions: list[dict[str, int]] = []
    try:
        for day in days:
            # MT5 MqlDateTime uses Sunday=0; datetime.weekday() uses Monday=0.
            day_of_week = (day.weekday() + 1) % 7
            for session_index in range(8):
                session = session_reader(symbol, day_of_week, session_index)
                if session is None:
                    break
                try:
                    start_seconds = getattr(session, "from")  # noqa: B009 - MT5 field is a keyword.
                    end_seconds = session.to
                except AttributeError:
                    try:
                        start_seconds, end_seconds = session["from"], session["to"]
                    except (KeyError, IndexError, TypeError):
                        break
                if (
                    isinstance(start_seconds, int)
                    and isinstance(end_seconds, int)
                    and 0 <= start_seconds < 86_400
                    and 0 <= end_seconds <= 86_400
                ):
                    sessions.append({
                        "day_of_week": day_of_week,
                        "session_index": session_index,
                        "from_seconds": start_seconds,
                        "to_seconds": end_seconds,
                    })
    except Exception:
        return {"available": False, "sessions": []}
    return {
        "available": bool(sessions),
        "time_basis": "MT5 session API values; UTC offset not inferred",
        "sessions": sessions[:16],
    }


def query_m15_history(api: Any, request: HistoryDiagnosticRequest) -> dict[str, Any]:
    timeframe_constant = getattr(api, "TIMEFRAME_M15", None)
    copy_rates = getattr(api, "copy_rates_range", None)
    if timeframe_constant is None or not callable(copy_rates):
        raise HistoryDiagnosticError("MT5_HISTORY_API_UNAVAILABLE")
    try:
        rates = copy_rates(request.symbol, timeframe_constant, request.start, request.end)
    except Exception:
        raise HistoryDiagnosticError("MT5_READ_FAILED") from None
    if rates is None:
        raise HistoryDiagnosticError("MT5_READ_FAILED")
    try:
        returned_count = len(rates)
    except (TypeError, OverflowError):
        raise HistoryDiagnosticError("MALFORMED_BROKER_RESULT") from None
    if returned_count > MAX_RETURNED_BARS:
        raise HistoryDiagnosticError("BAR_LIMIT_EXCEEDED")

    opens = [_rate_open_timestamp(row) for row in rates]
    if any(value < request.start or value > request.end for value in opens):
        raise HistoryDiagnosticError("BROKER_RESULT_OUTSIDE_REQUEST")
    if any(right <= left for left, right in zip(opens, opens[1:], strict=False)):
        raise HistoryDiagnosticError("MALFORMED_BROKER_RESULT")

    symbol_metadata: dict[str, Any] = {"name": request.symbol}
    symbol_info_reader = getattr(api, "symbol_info", None)
    if callable(symbol_info_reader):
        try:
            info = symbol_info_reader(request.symbol)
            if info is not None:
                trade_mode = getattr(info, "trade_mode", None)
                trade_mode_name = getattr(info, "trade_mode_name", None)
                if isinstance(trade_mode, int) and not isinstance(trade_mode, bool):
                    symbol_metadata["trade_mode"] = trade_mode
                if isinstance(trade_mode_name, str) and len(trade_mode_name) <= 32:
                    symbol_metadata["trade_mode_name"] = trade_mode_name
        except Exception:
            pass

    return {
        "ok": True,
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "requested_start": _format_utc(request.start),
        "requested_end": _format_utc(request.end),
        "bar_open_timestamps": [_format_utc(value) for value in opens],
        "returned_bar_count": len(opens),
        "symbol_metadata": symbol_metadata,
        "session_metadata": _session_metadata(api, request.symbol, request.start, request.end),
        "diagnostic_timestamp": _format_utc(datetime.now(UTC)),
    }


async def execute_history_diagnostic(
    gateway: Any,
    payload: object,
    *,
    active_symbol: str | None,
    runtime_connected: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    request_id = payload.get("request_id") if isinstance(payload, dict) else None
    if not runtime_connected or not getattr(gateway, "connected", False):
        return {"ok": False, "request_id": request_id, "error_code": "LIVE_NOT_CONNECTED"}
    try:
        request = validate_history_diagnostic_request(payload, now=now)
        if active_symbol is None or request.symbol != active_symbol:
            raise HistoryDiagnosticError("UNKNOWN_SYMBOL")
        query = getattr(gateway, "call_if_idle", None)
        if not callable(query):
            return {
                "ok": False,
                "request_id": request.request_id,
                "error_code": "GATEWAY_UNAVAILABLE",
            }
        result = await query(lambda api: query_m15_history(api, request))
        return {"request_id": request.request_id, **result}
    except HistoryDiagnosticError as exc:
        return {"ok": False, "request_id": request_id, "error_code": exc.code}
    except MT5GatewayBusyError:
        return {"ok": False, "request_id": request_id, "error_code": "MT5_GATEWAY_BUSY"}
    except Exception:
        return {"ok": False, "request_id": request_id, "error_code": "DIAGNOSTIC_UNAVAILABLE"}


HistoryDiagnosticHandler = Callable[[HistoryDiagnosticRequest], Awaitable[dict[str, Any]]]


def _send_response(connection: Any, response: dict[str, Any]) -> None:
    """Keep pipe serialization/client disconnect failures inside the IPC thread."""

    try:
        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_DIAGNOSTIC_MESSAGE_BYTES:
            encoded = json.dumps(
                {
                    "ok": False,
                    "request_id": response.get("request_id"),
                    "error_code": "RESPONSE_TOO_LARGE",
                },
                separators=(",", ":"),
            ).encode("utf-8")
        connection.send_bytes(encoded)
    except Exception:
        pass


class LiveHistoryDiagnosticServer:
    """Local Windows named-pipe server; all reads are dispatched to Live's loop."""

    def __init__(
        self,
        project_root: Path,
        loop: asyncio.AbstractEventLoop,
        handler: HistoryDiagnosticHandler,
        *,
        timeout_seconds: float = LIVE_HISTORY_DIAGNOSTIC_TIMEOUT_SECONDS,
    ) -> None:
        self.project_root = project_root
        self.loop = loop
        self.handler = handler
        self.timeout_seconds = timeout_seconds
        self.address = live_history_pipe_address(project_root)
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._accept_lock = threading.Lock()
        self._accepting = False

    def start(self) -> None:
        if os.name != "nt":
            raise HistoryDiagnosticError("WINDOWS_PIPE_UNAVAILABLE")
        if self._thread is not None:
            return
        try:
            self._listener = Listener(
                self.address, family="AF_PIPE", authkey=ipc_authkey(self.project_root)
            )
        except OSError:
            raise HistoryDiagnosticError("DIAGNOSTIC_PIPE_UNAVAILABLE") from None
        self._thread = threading.Thread(
            target=self._serve, name="xauusd-live-history-diagnostic", daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            listener, self._listener = self._listener, None
            if listener is not None:
                with suppress(Exception):
                    listener.close()
            self._stop.set()
            raise HistoryDiagnosticError("DIAGNOSTIC_PIPE_UNAVAILABLE") from None

    def close(self) -> None:
        self._stop.set()
        listener = self._listener
        thread = self._thread
        with self._accept_lock:
            wake_accept = self._accepting
        if listener is not None and thread is not None and thread.is_alive() and wake_accept:
            # PipeListener.close() does not cancel a handle already removed
            # from its pending-handle queue by accept(). Wake that accept with
            # a malformed local message; it is rejected before Live dispatch.
            with suppress(Exception):
                connection = Client(
                    self.address,
                    family="AF_PIPE",
                    authkey=ipc_authkey(self.project_root),
                )
                try:
                    connection.send_bytes(b'{"shutdown_wakeup":true}')
                finally:
                    connection.close()
        self._listener = None
        if listener is not None:
            with suppress(OSError):
                listener.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if not thread.is_alive():
                self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            payload = None
            with self._accept_lock:
                if self._stop.is_set():
                    return
                self._accepting = True
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                if not self._stop.is_set():
                    continue
                return
            finally:
                with self._accept_lock:
                    self._accepting = False
            try:
                raw_payload = connection.recv_bytes(MAX_DIAGNOSTIC_MESSAGE_BYTES)
                payload = json.loads(raw_payload)
                request = validate_history_diagnostic_request(payload)
                future = asyncio.run_coroutine_threadsafe(self.handler(request), self.loop)
                response = future.result(timeout=self.timeout_seconds)
                if (
                    not isinstance(response, dict)
                    or response.get("request_id") != request.request_id
                ):
                    response = {
                        "ok": False,
                        "request_id": request.request_id,
                        "error_code": "INVALID_LIVE_RESPONSE",
                    }
                _send_response(connection, response)
            except HistoryDiagnosticError as exc:
                _send_response(
                    connection,
                    {
                        "ok": False,
                        "request_id": (
                            payload.get("request_id")
                            if isinstance(payload, dict)
                            else None
                        ),
                        "error_code": exc.code,
                    },
                )
            except Exception:
                _send_response(
                    connection,
                    {
                        "ok": False,
                        "request_id": (
                            payload.get("request_id")
                            if isinstance(payload, dict)
                            else None
                        ),
                        "error_code": "DIAGNOSTIC_UNAVAILABLE",
                    },
                )
            finally:
                with suppress(OSError):
                    connection.close()


class LiveHistoryDiagnosticClient:
    """Local CLI client; it has no database or MT5 initialization path."""

    def __init__(self, project_root: Path, *, timeout_seconds: float = 25.0) -> None:
        self.project_root = project_root
        self.timeout_seconds = timeout_seconds

    def request(self, payload: object) -> dict[str, Any]:
        request = validate_history_diagnostic_request(payload)
        try:
            connection = Client(
                live_history_pipe_address(self.project_root),
                family="AF_PIPE",
                authkey=ipc_authkey(self.project_root),
            )
        except (OSError, EOFError):
            raise HistoryDiagnosticError("LIVE_DIAGNOSTIC_UNAVAILABLE") from None
        try:
            connection.send_bytes(json.dumps({
                "request_id": request.request_id,
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "start": _format_utc(request.start),
                "end": _format_utc(request.end),
            }, separators=(",", ":")).encode("utf-8"))
            if not connection.poll(self.timeout_seconds):
                raise HistoryDiagnosticError("LIVE_DIAGNOSTIC_TIMEOUT")
            response = json.loads(connection.recv_bytes(MAX_DIAGNOSTIC_MESSAGE_BYTES))
        except HistoryDiagnosticError:
            raise
        except (OSError, EOFError, ValueError, UnicodeDecodeError):
            raise HistoryDiagnosticError("LIVE_DIAGNOSTIC_UNAVAILABLE") from None
        finally:
            with suppress(OSError):
                connection.close()
        if (
            not isinstance(response, dict)
            or response.get("request_id") != request.request_id
            or not isinstance(response.get("ok"), bool)
        ):
            raise HistoryDiagnosticError("INVALID_LIVE_RESPONSE")
        if not response["ok"]:
            code = response.get("error_code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z_]{1,48}", code):
                code = "DIAGNOSTIC_UNAVAILABLE"
            raise HistoryDiagnosticError(code)
        allowed = {
            "ok", "request_id", "symbol", "timeframe", "requested_start", "requested_end",
            "bar_open_timestamps", "returned_bar_count", "symbol_metadata", "session_metadata",
            "diagnostic_timestamp",
        }
        if set(response) != allowed:
            raise HistoryDiagnosticError("INVALID_LIVE_RESPONSE")
        return response


__all__ = [
    "HistoryDiagnosticError",
    "HistoryDiagnosticRequest",
    "LiveHistoryDiagnosticClient",
    "LiveHistoryDiagnosticServer",
    "MAX_HISTORY_RANGE",
    "MAX_RETURNED_BARS",
    "execute_history_diagnostic",
    "live_history_pipe_address",
    "query_m15_history",
    "validate_history_diagnostic_request",
]
