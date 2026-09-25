from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.event_loop_diagnostics import EventLoopLagDiagnostics
from services.shadow_diagnostics import ShadowDiagnostics


def test_event_loop_probe_tracks_latest_and_max_lag_without_history() -> None:
    probe = EventLoopLagDiagnostics(interval_ms=250, threshold_ms=1_000)
    origin = datetime(2026, 9, 25, tzinfo=UTC)

    assert probe.record_sample(
        expected_wake_monotonic=10.0,
        actual_wake_monotonic=10.01,
        observed_at=origin,
    ) == pytest.approx(10)
    first = probe.snapshot()
    assert first["latest_event_loop_lag_ms"] == pytest.approx(10)
    assert first["latest_event_loop_lag_observed_at"] == origin.isoformat()
    assert first["max_event_loop_lag_ms"] == pytest.approx(10)
    assert first["max_event_loop_lag_observed_at"] == origin.isoformat()

    probe.record_sample(
        expected_wake_monotonic=11.0,
        actual_wake_monotonic=12.25,
        observed_at=origin + timedelta(seconds=1),
    )
    second = probe.snapshot()
    assert second["max_event_loop_lag_ms"] == 1_250
    assert second["max_event_loop_lag_observed_at"] == (
        origin + timedelta(seconds=1)
    ).isoformat()

    probe.record_sample(
        expected_wake_monotonic=13.0,
        actual_wake_monotonic=13.1,
        observed_at=origin + timedelta(seconds=2),
    )

    snapshot = probe.snapshot()
    assert snapshot["latest_event_loop_lag_ms"] == pytest.approx(100)
    assert snapshot["latest_event_loop_lag_observed_at"] == (
        origin + timedelta(seconds=2)
    ).isoformat()
    assert snapshot["max_event_loop_lag_ms"] == 1_250
    assert snapshot["max_event_loop_lag_observed_at"] == (
        origin + timedelta(seconds=1)
    ).isoformat()
    assert snapshot["latest_threshold_exceeded"] is False
    assert snapshot["measurement_interval_ms"] == 250
    assert snapshot["diagnostic_threshold_ms"] == 1_000
    assert len(snapshot) == 7
    assert not any("history" in key or "samples" in key for key in snapshot)


def test_event_loop_snapshot_omits_unavailable_sample_evidence() -> None:
    snapshot = EventLoopLagDiagnostics(interval_ms=250, threshold_ms=1_000).snapshot()

    assert "latest_event_loop_lag_ms" not in snapshot
    assert "latest_event_loop_lag_observed_at" not in snapshot
    assert "max_event_loop_lag_ms" not in snapshot
    assert "max_event_loop_lag_observed_at" not in snapshot


def test_event_loop_probe_rejects_invalid_diagnostic_configuration() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        EventLoopLagDiagnostics(interval_ms=0, threshold_ms=1)


def test_shadow_diagnostics_expose_active_operation_then_clear_it() -> None:
    diagnostics = ShadowDiagnostics()
    started_at = datetime(2026, 9, 25, tzinfo=UTC)
    token = diagnostics.begin(
        "DECISION_EVALUATION", started_at=started_at, ownership="ON_EVENT_LOOP"
    )

    active = diagnostics.snapshot()
    assert active["current_operation"] == "DECISION_EVALUATION"
    assert active["current_operation_started_at"] == started_at.isoformat()
    assert active["current_operation_ownership"] == "ON_EVENT_LOOP"

    diagnostics.finish(token, completed_at=started_at + timedelta(seconds=12))
    completed = diagnostics.snapshot()
    assert "current_operation" not in completed
    assert completed["last_completed_operation"]["operation"] == "DECISION_EVALUATION"
    assert completed["timings"]["DECISION_EVALUATION"]["last_completed_at"] == (
        started_at + timedelta(seconds=12)
    ).isoformat()
    assert completed["timings"]["DECISION_EVALUATION"]["max_duration_ms"] >= 0
