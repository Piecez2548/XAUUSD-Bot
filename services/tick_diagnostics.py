"""Bounded in-memory timing evidence for the Live tick polling path."""

from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any


class TickPollingDiagnostics:
    """Keep only the latest tick-path evidence; never retain per-poll history."""

    def __init__(self, *, stale_threshold_seconds: float, slow_threshold_seconds: float) -> None:
        self.stale_threshold_ms = stale_threshold_seconds * 1_000
        self.slow_threshold_ms = slow_threshold_seconds * 1_000
        self._lock = Lock()
        self._previous_attempt_monotonic: float | None = None
        self._active: dict[str, Any] | None = None
        self._last: dict[str, Any] = {}
        self._consecutive_read_failures = 0
        self._last_error_category: str | None = None
        self._last_failed_read_at: datetime | None = None
        self._persistence: dict[str, tuple[float, str | None]] = {}
        self._metrics: dict[str, float] = {}

    def begin_attempt(self, started_at: datetime, started_monotonic: float) -> None:
        with self._lock:
            gap = None
            if self._previous_attempt_monotonic is not None:
                gap = max(0.0, (started_monotonic - self._previous_attempt_monotonic) * 1_000)
            self._previous_attempt_monotonic = started_monotonic
            self._active = {
                "attempt_started_at": started_at,
                "attempt_started_monotonic": started_monotonic,
                "loop_gap_ms": gap,
                "stage": "WAITING_FOR_MT5_ACCESS",
                "stage_started_at": started_at,
                "stage_started_monotonic": started_monotonic,
                "tick_read_started_monotonic": None,
                "tick_read_duration_ms": None,
                "source_timestamp": None,
                "tick_read_failed": False,
                "gateway_access_acquired_monotonic": None,
                "gateway_completed_monotonic": None,
            }

    def mark_stage(self, stage: str, at: datetime, monotonic: float) -> None:
        with self._lock:
            if self._active is None:
                return
            self._active["stage"] = stage
            self._active["stage_started_at"] = at
            self._active["stage_started_monotonic"] = monotonic
            if stage == "MT5_GATEWAY_OPERATION":
                self._active["gateway_access_acquired_monotonic"] = monotonic
            elif stage == "MT5_GATEWAY_OPERATION_COMPLETED":
                self._active["gateway_completed_monotonic"] = monotonic
            elif stage == "MT5_TICK_READ":
                self._active["tick_read_started_monotonic"] = monotonic

    def record_tick_read(
        self,
        *,
        source_timestamp: datetime,
        completed_at: datetime,
        completed_monotonic: float,
    ) -> None:
        with self._lock:
            if self._active is None:
                return
            tick_started = self._active.get("tick_read_started_monotonic")
            self._active["tick_read_duration_ms"] = (
                max(0.0, (completed_monotonic - tick_started) * 1_000)
                if tick_started is not None
                else None
            )
            self._active["source_timestamp"] = source_timestamp
            self._consecutive_read_failures = 0
            self._last_error_category = None
            source_age_ms = (completed_at - source_timestamp).total_seconds() * 1_000
            self._last.update(
                {
                    "last_tick_read_at": completed_at,
                    "mt5_tick_source_timestamp": source_timestamp,
                    "last_read_classification": (
                        "SOURCE_TICK_STALE" if source_age_ms > self.stale_threshold_ms else "NORMAL"
                    ),
                }
            )

    def record_tick_read_failure(
        self, failed_at: datetime, failed_monotonic: float, error_category: str
    ) -> None:
        with self._lock:
            self._consecutive_read_failures += 1
            self._last_error_category = error_category
            self._last_failed_read_at = failed_at
            self._last["last_read_classification"] = "READ_FAILURE"
            if self._active is not None:
                started = self._active.get("tick_read_started_monotonic")
                self._active["tick_read_duration_ms"] = (
                    max(0.0, (failed_monotonic - started) * 1_000) if started is not None else None
                )
                self._active["tick_read_failed"] = True

    def finish_attempt(
        self,
        *,
        completed_at: datetime,
        completed_monotonic: float,
        observed_at: datetime | None,
        gateway_wait_ms: float | None,
        gateway_operation_ms: float | None,
        positions_read_ms: float | None,
        error_category: str | None = None,
        tick_read_failed: bool = False,
    ) -> None:
        with self._lock:
            active = self._active
            if active is None:
                return
            started = active["attempt_started_monotonic"]
            acquired = active.get("gateway_access_acquired_monotonic")
            gateway_completed = active.get("gateway_completed_monotonic")
            if gateway_wait_ms is None and acquired is not None:
                gateway_wait_ms = max(0.0, (acquired - started) * 1_000)
            if (
                gateway_operation_ms is None
                and acquired is not None
                and gateway_completed is not None
            ):
                gateway_operation_ms = max(0.0, (gateway_completed - acquired) * 1_000)
            self._last.update(active)
            self._last.update(
                {
                    "attempt_completed_at": completed_at,
                    "read_duration_ms": max(0.0, (completed_monotonic - started) * 1_000),
                    "last_successful_observation_at": observed_at
                    or self._last.get("last_successful_observation_at"),
                    "gateway_wait_ms": gateway_wait_ms,
                    "gateway_operation_ms": gateway_operation_ms,
                    "positions_read_duration_ms": positions_read_ms,
                    "last_attempt_error_category": error_category,
                }
            )
            if tick_read_failed and not active.get("tick_read_failed"):
                self._consecutive_read_failures += 1
                self._last_error_category = error_category
                self._last_failed_read_at = completed_at
            self._active = None

    def record_timing(self, name: str, duration_ms: float, event_type: str | None = None) -> None:
        with self._lock:
            self._persistence[name] = (max(0.0, duration_ms), event_type)

    def record_metric(self, name: str, value: float) -> None:
        with self._lock:
            self._metrics[name] = max(0.0, value)

    def seed_observation(self, observed_at: datetime, source_timestamp: datetime) -> None:
        with self._lock:
            if self._last.get("last_successful_observation_at") is None:
                self._last["last_successful_observation_at"] = observed_at
                self._last["mt5_tick_source_timestamp"] = source_timestamp
                self._last["diagnostic_classification"] = "INITIAL_SYNC"

    def snapshot(self, *, now: datetime, monotonic: float) -> dict[str, str | int | float]:
        with self._lock:
            last = dict(self._last)
            active = dict(self._active) if self._active is not None else None
            failures = self._consecutive_read_failures
            last_error = self._last_error_category
            last_failed = self._last_failed_read_at
            persistence = dict(self._persistence)
            metrics = dict(self._metrics)

        values: dict[str, str | int | float] = {}

        def put(name: str, value: Any) -> None:
            if value is None:
                return
            if isinstance(value, datetime):
                values[name] = value.isoformat()
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                values[name] = value

        put(
            "attempt_started_at",
            active.get("attempt_started_at") if active else last.get("attempt_started_at"),
        )
        put("attempt_completed_at", last.get("attempt_completed_at"))
        observed = last.get("last_successful_observation_at")
        put("last_successful_observation_at", observed)
        put("local_observed_at", observed)
        source = (
            active.get("source_timestamp") or last.get("mt5_tick_source_timestamp")
            if active
            else last.get("mt5_tick_source_timestamp")
        )
        put("mt5_tick_source_timestamp", source)
        put("last_mt5_tick_source_timestamp", source)
        if observed is not None:
            put("observation_age_ms", max(0.0, (now - observed).total_seconds() * 1_000))
        if source is not None and observed is not None:
            put("source_tick_age_ms", max(0.0, (observed - source).total_seconds() * 1_000))
        put(
            "read_duration_ms",
            max(0.0, (monotonic - active["attempt_started_monotonic"]) * 1_000)
            if active
            else last.get("read_duration_ms"),
        )
        put("last_loop_gap_ms", active.get("loop_gap_ms") if active else last.get("loop_gap_ms"))
        acquired = active.get("gateway_access_acquired_monotonic") if active else None
        if active:
            wait_elapsed = monotonic - active["attempt_started_monotonic"]
            put(
                "waiting_for_mt5_access_ms",
                max(0.0, wait_elapsed * 1_000)
                if acquired is None
                else max(0.0, (acquired - active["attempt_started_monotonic"]) * 1_000),
            )
            put(
                "mt5_gateway_operation_ms",
                max(0.0, (monotonic - acquired) * 1_000) if acquired is not None else None,
            )
        else:
            put("waiting_for_mt5_access_ms", last.get("gateway_wait_ms"))
            put("mt5_gateway_operation_ms", last.get("gateway_operation_ms"))
        tick_started = active.get("tick_read_started_monotonic") if active else None
        put(
            "mt5_tick_read_ms",
            max(0.0, (monotonic - tick_started) * 1_000)
            if active and active.get("stage") == "MT5_TICK_READ" and tick_started is not None
            else active.get("tick_read_duration_ms")
            if active
            else last.get("tick_read_duration_ms"),
        )
        put(
            "positions_read_ms",
            active.get("positions_read_duration_ms")
            if active
            else last.get("positions_read_duration_ms"),
        )
        put("consecutive_read_failures", failures)
        put("last_failed_read_at", last_failed)
        put("last_error_category", last_error)
        put("last_read_classification", last.get("last_read_classification"))
        put("last_attempt_error_category", last.get("last_attempt_error_category"))
        if active:
            put("active_stage", active.get("stage"))
            stage_started = active.get("stage_started_monotonic")
            if stage_started is not None:
                put("active_stage_elapsed_ms", max(0.0, (monotonic - stage_started) * 1_000))
        values["diagnostic_classification"] = self._classify(active, last, now, monotonic, failures)
        for name, (duration, event_type) in persistence.items():
            put(f"last_{name}_duration_ms", duration)
            if event_type:
                put("last_event_type", event_type)
        for name, value in metrics.items():
            put(name, value)
        put("threshold_seconds", self.stale_threshold_ms / 1_000)
        return values

    def _classify(
        self,
        active: dict[str, Any] | None,
        last: dict[str, Any],
        now: datetime,
        monotonic: float,
        failures: int,
    ) -> str:
        if failures or (active is None and last.get("last_attempt_error_category")):
            return "READ_FAILURE"
        if active is not None:
            elapsed = max(0.0, (monotonic - active["stage_started_monotonic"]) * 1_000)
            if active["stage"] == "WAITING_FOR_MT5_ACCESS" and elapsed >= self.slow_threshold_ms:
                return "MT5_ACCESS_WAIT"
            if (
                active["stage"] in {"MT5_TICK_READ", "MT5_POSITION_READ"}
                and elapsed >= self.slow_threshold_ms
            ):
                return "MT5_READ_DELAY"
            if (
                active.get("loop_gap_ms") is not None
                and active["loop_gap_ms"] >= self.slow_threshold_ms
            ):
                return "POLL_DELAY"
        else:
            started = last.get("attempt_started_monotonic")
            if started is not None and (monotonic - started) * 1_000 >= self.slow_threshold_ms:
                return "POLL_DELAY"
            observed = last.get("last_successful_observation_at")
            if (
                observed is not None
                and (now - observed).total_seconds() * 1_000 > self.stale_threshold_ms
            ):
                return "POLL_DELAY"
        source = (
            (active.get("source_timestamp") or last.get("mt5_tick_source_timestamp"))
            if active
            else last.get("mt5_tick_source_timestamp")
        )
        if source is not None and (now - source).total_seconds() * 1_000 > self.stale_threshold_ms:
            return "SOURCE_TICK_STALE"
        return "NORMAL"
