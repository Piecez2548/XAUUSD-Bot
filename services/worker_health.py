"""Shared freshness policy for persisted live-worker health records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

CONNECTED = "CONNECTED"
DEGRADED = "DEGRADED"
UNKNOWN = "UNKNOWN"
CATCHING_UP = "CATCHING_UP"


def worker_health_ttl(interval_seconds: float) -> float:
    """Return a bounded heartbeat TTL that tolerates normal scheduler jitter."""

    return max(60.0, float(interval_seconds) * 4.0)


def derive_worker_state(
    *,
    status: str | None,
    timestamp: datetime | None,
    interval_seconds: float,
    now: datetime | None = None,
) -> str:
    """Derive one public worker state from the authoritative persisted row."""

    if timestamp is None:
        return UNKNOWN
    current = now or datetime.now(UTC)
    observed = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    age = max(0.0, (current - observed).total_seconds())
    ttl = worker_health_ttl(interval_seconds)
    normalized = (status or "").upper()
    if normalized == "DISABLED":
        return "DISABLED"
    if age <= ttl and normalized in {CONNECTED, CATCHING_UP, "ERROR"}:
        if normalized == "ERROR":
            return "ERROR"
        return normalized
    if age <= ttl * 2 and normalized in {CONNECTED, CATCHING_UP, DEGRADED, "ERROR"}:
        if normalized == "ERROR":
            return "ERROR"
        return DEGRADED
    return UNKNOWN


def worker_health_payload(
    row: Any | None,
    *,
    interval_seconds: float,
    now: datetime | None = None,
) -> dict[str, object]:
    """Serialize a safe worker health view without credentials or provider data."""

    current = now or datetime.now(UTC)
    timestamp = getattr(row, "timestamp", None) if row is not None else None
    state = derive_worker_state(
        status=getattr(row, "status", None) if row is not None else None,
        timestamp=timestamp,
        interval_seconds=interval_seconds,
        now=current,
    )
    age = None
    if timestamp is not None:
        observed = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
        age = max(0.0, (current - observed).total_seconds())
    metadata = getattr(row, "metadata_json", None) or {}
    return {
        "state": state,
        "observed_at": timestamp,
        "age_seconds": age,
        "last_success_at": metadata.get("last_success_at"),
        "last_failed_at": metadata.get("last_failed_at"),
        "last_received_candle_at": metadata.get("last_received_candle_at"),
        "last_processed_candle_at": metadata.get("last_processed_candle_at"),
        "last_decision_at": metadata.get("last_decision_at"),
        "failure_count": metadata.get("failure_count", 0),
        "queue_depth": metadata.get("queue_depth", 0),
        "queue_capacity": metadata.get("queue_capacity", 8),
        "deferred_count": metadata.get("deferred_count", 0),
        "catchup_pending_count": metadata.get("catchup_pending_count", 0),
        "total_backlog": metadata.get("total_backlog", metadata.get("queue_depth", 0)),
        "processing_lag_seconds": metadata.get("processing_lag_seconds"),
        "latest_available_m5": metadata.get("latest_available_m5"),
        "latest_received_m5": metadata.get("latest_received_m5")
        or metadata.get("last_received_candle_at"),
        "latest_processed_m5": metadata.get("latest_processed_m5")
        or metadata.get("last_processed_candle_at"),
        "latest_decision_m5": metadata.get("latest_decision_m5")
        or metadata.get("last_decision_at"),
        "last_failure": metadata.get("last_failure", metadata.get("error_category")),
        "error_category": metadata.get("error_category"),
        "session_id": metadata.get("session_id"),
        "last_closed_m5": metadata.get("last_closed_m5"),
        "last_closed_m15": metadata.get("last_closed_m15"),
        "last_closed_h1": metadata.get("last_closed_h1"),
        "last_zone_created": metadata.get("last_zone_created"),
        "last_signal": metadata.get("last_signal"),
        "open_shadow_trades": metadata.get("open_shadow_trades", 0),
        "total_forward_signals": metadata.get("total_forward_signals", 0),
        "execution_allowed": False,
        "message": getattr(row, "message", None) if row is not None else None,
    }
