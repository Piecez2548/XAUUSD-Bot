"""Failure-isolated Telegram notification subscriber with bounded retry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Protocol

import httpx

from domain.events import DomainEvent, EventSeverity, EventType
from notifications.templates import format_telegram_event, telegram_test_message

Sleep = Callable[[float], Awaitable[None]]


class TelegramHealthReporter(Protocol):
    def record_telegram_outcome(
        self,
        outcome: str,
        *,
        latency_ms: float | None = None,
    ) -> str: ...


class TelegramNotifier:
    """Operator-alert subscriber; routine telemetry is intentionally silent."""

    NOTIFIABLE_EVENT_TYPES = frozenset(
        {
            EventType.SYSTEM_STARTED,
            EventType.SYSTEM_STOPPED,
            EventType.SYSTEM_LIVE_STARTED,
            EventType.SYSTEM_LIVE_STOPPED,
            EventType.MT5_CONNECTED,
            EventType.MT5_DISCONNECTED,
            EventType.MT5_RECONNECTED,
            EventType.RISK_SNAPSHOT_CREATED,
            EventType.SHADOW_DECISION_CREATED,
            EventType.TRADE_REQUESTED,
            EventType.TRADE_APPROVED,
            EventType.TRADE_REJECTED,
            EventType.POSITION_OPENED,
            EventType.POSITION_ADDED,
            EventType.POSITION_MODIFIED,
            EventType.SL_MODIFIED,
            EventType.TP_MODIFIED,
            EventType.POSITION_PARTIALLY_CLOSED,
            EventType.POSITION_CLOSED,
            EventType.MANUAL_CLOSE,
            EventType.AI_CLOSE,
            EventType.TAKE_PROFIT,
            EventType.STOP_LOSS,
            EventType.RISK_LIMIT_REJECTED,
            EventType.AI_UNAVAILABLE,
            EventType.NEWS_SERVICE_UNAVAILABLE,
            EventType.KILL_SWITCH_ACTIVATED,
            EventType.SYSTEM_ERROR,
            EventType.POSITION_OBSERVED_OPEN,
            EventType.POSITION_WITHOUT_STOP_LOSS,
            EventType.RISK_UNBOUNDED,
            EventType.RISK_BACK_WITHIN_BOUNDS,
            EventType.HISTORY_SYNC_FAILED,
            EventType.HISTORY_SYNC_RECOVERED,
            EventType.DATA_STALE,
        }
    )

    def __init__(
        self,
        *,
        enabled: bool,
        bot_token: str | None,
        chat_id: str | None,
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        logger: logging.Logger | None = None,
        health_reporter: TelegramHealthReporter | None = None,
    ) -> None:
        self.enabled = enabled
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._client = client
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)
        self._health_reporter = health_reporter

    @property
    def configured(self) -> bool:
        return self.enabled and bool(
            self._bot_token and self._bot_token.strip() and self._chat_id and self._chat_id.strip()
        )

    async def handle(self, event: DomainEvent) -> None:
        if not self.configured or event.event_type not in self.NOTIFIABLE_EVENT_TYPES:
            return
        if self._is_routine_lifecycle_event(event):
            return
        await self.send(format_telegram_event(event))

    @staticmethod
    def _is_routine_lifecycle_event(event: DomainEvent) -> bool:
        """Keep expected startup/stop telemetry inside the lifecycle summary.

        Persistence still receives every event through the database subscriber.
        Only normal informational child notifications are coalesced here;
        warnings, errors, disconnects and execution alerts remain independent.
        """

        if event.event_type in {
            EventType.SYSTEM_LIVE_STARTED,
            EventType.SYSTEM_LIVE_STOPPED,
        }:
            return True
        if event.severity != EventSeverity.INFO:
            return False
        return event.event_type in {
            EventType.MT5_CONNECTED,
            EventType.RISK_SNAPSHOT_CREATED,
        }

    async def send_test(self) -> bool:
        if not self.configured:
            self._record_health("disabled" if not self.enabled else "configuration_error")
            self._logger.warning("Telegram test skipped: integration is disabled or incomplete")
            return False
        return await self.send(telegram_test_message())

    async def send(self, message: str) -> bool:
        if not self.configured:
            self._record_health("disabled" if not self.enabled else "configuration_error")
            return False
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        started_at = perf_counter()
        try:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    response = await client.post(
                        url,
                        json={"chat_id": self._chat_id, "text": message},
                    )
                    response.raise_for_status()
                    self._record_health(
                        "success",
                        latency_ms=(perf_counter() - started_at) * 1_000,
                    )
                    return True
                except httpx.HTTPStatusError as exc:
                    if 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                        self._logger.warning("Telegram API rejected a delivery request")
                        self._record_health(
                            "unrecoverable_failure",
                            latency_ms=(perf_counter() - started_at) * 1_000,
                        )
                        return False
                    self._logger.warning(
                        "Telegram delivery failed temporarily (attempt %d/%d)",
                        attempt,
                        self._max_attempts,
                    )
                except (httpx.HTTPError, TimeoutError):
                    self._logger.warning(
                        "Telegram delivery failed temporarily (attempt %d/%d)",
                        attempt,
                        self._max_attempts,
                    )
                    if attempt < self._max_attempts:
                        await self._sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                if attempt < self._max_attempts:
                    await self._sleep(0.5 * (2 ** (attempt - 1)))
            self._record_health(
                "recoverable_failure",
                latency_ms=(perf_counter() - started_at) * 1_000,
            )
            return False
        finally:
            if owns_client:
                await client.aclose()

    def _record_health(self, outcome: str, *, latency_ms: float | None = None) -> None:
        if self._health_reporter is None:
            return
        try:
            self._health_reporter.record_telegram_outcome(outcome, latency_ms=latency_ms)
        except Exception:
            self._logger.exception("Telegram health persistence failed")
