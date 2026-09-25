"""Bounded, diagnostic-only event-loop scheduling lag measurements."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime


class EventLoopLagDiagnostics:
    """Retain only the latest and maximum event-loop wake-up lag."""

    def __init__(self, *, interval_ms: float, threshold_ms: float) -> None:
        if interval_ms <= 0 or threshold_ms <= 0:
            raise ValueError("event-loop diagnostic interval and threshold must be positive")
        self.interval_ms = interval_ms
        self.threshold_ms = threshold_ms
        self._latest_lag_ms: float | None = None
        self._latest_lag_observed_at: datetime | None = None
        self._max_lag_ms: float | None = None
        self._max_lag_observed_at: datetime | None = None

    def record_sample(
        self, *, expected_wake_monotonic: float, actual_wake_monotonic: float,
        observed_at: datetime,
    ) -> float:
        lag_ms = max(0.0, (actual_wake_monotonic - expected_wake_monotonic) * 1_000)
        self._latest_lag_ms = lag_ms
        self._latest_lag_observed_at = observed_at
        if self._max_lag_ms is None or lag_ms > self._max_lag_ms:
            self._max_lag_ms = lag_ms
            self._max_lag_observed_at = observed_at
        return lag_ms

    async def run(self, stop: asyncio.Event) -> None:
        """Sample expected versus actual wakes without persistence or event writes."""

        loop = asyncio.get_running_loop()
        expected_wake = loop.time() + self.interval_ms / 1_000
        while not stop.is_set():
            await asyncio.sleep(max(0.0, expected_wake - loop.time()))
            if stop.is_set():
                return
            actual_wake = loop.time()
            self.record_sample(
                expected_wake_monotonic=expected_wake,
                actual_wake_monotonic=actual_wake,
                observed_at=datetime.now(UTC),
            )
            # Rebase after a delayed wake rather than spinning to catch up.
            expected_wake = actual_wake + self.interval_ms / 1_000

    def snapshot(self) -> dict[str, str | float | bool]:
        result: dict[str, str | float | bool] = {
            "measurement_interval_ms": self.interval_ms,
            "diagnostic_threshold_ms": self.threshold_ms,
        }
        if self._latest_lag_ms is not None:
            result["latest_event_loop_lag_ms"] = self._latest_lag_ms
            if self._latest_lag_observed_at is not None:
                result["latest_event_loop_lag_observed_at"] = (
                    self._latest_lag_observed_at.isoformat()
                )
            result["latest_threshold_exceeded"] = self._latest_lag_ms >= self.threshold_ms
        if self._max_lag_ms is not None:
            result["max_event_loop_lag_ms"] = self._max_lag_ms
        if self._max_lag_observed_at is not None:
            result["max_event_loop_lag_observed_at"] = self._max_lag_observed_at.isoformat()
        return result
