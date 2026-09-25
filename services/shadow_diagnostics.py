"""Bounded in-memory diagnostics for the legacy Shadow decision worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any


@dataclass(slots=True)
class OperationToken:
    operation: str
    started_at: datetime
    started_monotonic: float
    ownership: str
    operation_id: int
    previous: dict[str, Any] | None


class ShadowDiagnostics:
    """Keep a fixed set of operation summaries and one active operation."""

    OPERATIONS = (
        "QUEUE_WAIT",
        "DECISION_EVALUATION",
        "DECISION_PERSISTENCE",
        "EVENT_PUBLISH",
        "CATCH_UP_LOAD",
        "CATCH_UP_PROCESSING",
        "TOTAL_ITEM_PROCESSING",
    )
    MAX_QUEUE_FULL_COUNT = 2_147_483_647

    def __init__(self) -> None:
        self._latest: dict[str, dict[str, Any]] = {}
        self._maximum: dict[str, dict[str, Any]] = {}
        self._current: dict[str, Any] | None = None
        self._last_completed: dict[str, Any] | None = None
        self._next_operation_id = 0
        self._queue_full_count = 0
        self._queue_size = 0
        self._queue_capacity = 0
        self._backlog_count = 0
        self._catch_up_count = 0
        self._processing_lag_seconds: float | None = None
        self._oldest_queued_age_seconds: float | None = None

    def begin(self, operation: str, *, started_at: datetime, ownership: str) -> OperationToken:
        if operation not in self.OPERATIONS:
            raise ValueError("unsupported Shadow diagnostic operation")
        if ownership not in {"ON_EVENT_LOOP", "OFFLOADED_WORKER"}:
            raise ValueError("unsupported diagnostic ownership")
        self._next_operation_id += 1
        token = OperationToken(
            operation=operation,
            started_at=started_at,
            started_monotonic=perf_counter(),
            ownership=ownership,
            operation_id=self._next_operation_id,
            previous=dict(self._current) if self._current is not None else None,
        )
        self._current = {
            "operation": operation,
            "started_at": started_at,
            "started_monotonic": token.started_monotonic,
            "ownership": ownership,
            "operation_id": token.operation_id,
        }
        return token

    def finish(self, token: OperationToken | None, *, completed_at: datetime) -> None:
        if token is None:
            return
        duration_ms = max(0.0, (perf_counter() - token.started_monotonic) * 1_000)
        try:
            self.record_duration(
                token.operation,
                duration_ms=duration_ms,
                started_at=token.started_at,
                completed_at=completed_at,
                ownership=token.ownership,
            )
        finally:
            if self._current is not None and self._current["operation_id"] == token.operation_id:
                self._current = token.previous

    def abort(self, token: OperationToken | None) -> None:
        if (
            token is not None
            and self._current is not None
            and self._current["operation_id"] == token.operation_id
        ):
            self._current = token.previous

    def record_duration(
        self,
        operation: str,
        *,
        duration_ms: float,
        started_at: datetime,
        completed_at: datetime,
        ownership: str,
    ) -> None:
        if operation not in self.OPERATIONS or ownership not in {
            "ON_EVENT_LOOP",
            "OFFLOADED_WORKER",
        }:
            raise ValueError("unsupported Shadow diagnostic duration")
        duration_ms = max(0.0, duration_ms)
        entry = {
            "latest_duration_ms": duration_ms,
            "max_duration_ms": duration_ms,
            "last_started_at": started_at.isoformat(),
            "last_completed_at": completed_at.isoformat(),
            "ownership": ownership,
        }
        self._latest[operation] = entry
        previous_max = self._maximum.get(operation)
        if previous_max is None or duration_ms >= previous_max["max_duration_ms"]:
            self._maximum[operation] = {
                **entry,
                "max_duration_ms": duration_ms,
            }
        else:
            entry["max_duration_ms"] = previous_max["max_duration_ms"]
        self._last_completed = {
            "operation": operation,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "ownership": ownership,
        }

    def record_queue_full(self) -> None:
        self._queue_full_count = min(self.MAX_QUEUE_FULL_COUNT, self._queue_full_count + 1)

    def record_pressure(
        self,
        *,
        queue_size: int,
        queue_capacity: int,
        backlog_count: int,
        catch_up_count: int,
        processing_lag_seconds: float | None,
        oldest_queued_age_seconds: float | None = None,
    ) -> None:
        self._queue_size = max(0, queue_size)
        self._queue_capacity = max(0, queue_capacity)
        self._backlog_count = max(0, backlog_count)
        self._catch_up_count = max(0, catch_up_count)
        self._processing_lag_seconds = (
            max(0.0, processing_lag_seconds) if processing_lag_seconds is not None else None
        )
        self._oldest_queued_age_seconds = (
            max(0.0, oldest_queued_age_seconds)
            if oldest_queued_age_seconds is not None
            else None
        )

    def snapshot(self, *, monotonic: float | None = None) -> dict[str, Any]:
        now = perf_counter() if monotonic is None else monotonic
        timings: dict[str, dict[str, Any]] = {}
        for operation in self.OPERATIONS:
            latest = self._latest.get(operation)
            maximum = self._maximum.get(operation)
            if latest is None:
                continue
            timings[operation] = {
                **latest,
                "max_duration_ms": (
                    maximum["max_duration_ms"]
                    if maximum
                    else latest["max_duration_ms"]
                ),
                "max_last_completed_at": (
                    maximum["last_completed_at"]
                    if maximum
                    else latest["last_completed_at"]
                ),
                "max_ownership": maximum["ownership"] if maximum else latest["ownership"],
            }
        result: dict[str, Any] = {
            "timings": timings,
            "queue_size": self._queue_size,
            "queue_capacity": self._queue_capacity,
            "backlog_count": self._backlog_count,
            "catch_up_count": self._catch_up_count,
            "queue_full_count_since_start": self._queue_full_count,
        }
        if self._processing_lag_seconds is not None:
            result["processing_lag_seconds"] = self._processing_lag_seconds
        if self._oldest_queued_age_seconds is not None:
            result["oldest_queued_age_seconds"] = self._oldest_queued_age_seconds
        if self._current is not None:
            result["current_operation"] = self._current["operation"]
            result["current_operation_started_at"] = self._current["started_at"].isoformat()
            result["current_operation_ownership"] = self._current["ownership"]
            result["current_operation_elapsed_ms"] = max(
                0.0, (now - self._current["started_monotonic"]) * 1_000
            )
        if self._last_completed is not None:
            result["last_completed_operation"] = dict(self._last_completed)
        return result
