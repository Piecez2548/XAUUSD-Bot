from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from research.intelligence_evaluation import (
    EVALUATION_VERSION,
    EvaluationInputError,
    evaluate_records,
    observation_from_mapping,
    observations_from_persisted_rows,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def record(
    number: int,
    *,
    score: float | None = 70.0,
    outcome: str | None = "TP_HIT",
    realized_r: float | None = 1.0,
    disposition: str = "ALERT",
    baseline_positive: bool | None = True,
    evidence_timestamp: datetime | None = None,
    manual_state: str | None = None,
    components: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    timestamp = BASE_TIME + timedelta(hours=number)
    evidence = [
        {
            "type": "pair_zone_state",
            "source": "pair_zone_v1",
            "timestamp": (evidence_timestamp or timestamp).isoformat(),
            "polarity": "SUPPORTIVE",
        }
    ]
    if manual_state:
        evidence.append(
            {"type": "manual_ob_state", "value": manual_state, "timestamp": timestamp.isoformat()}
        )
    return {
        "observation_id": f"candidate-{number}",
        "symbol": "XAUUSDm",
        "timeframe": "M15/M5",
        "timestamp": timestamp.isoformat(),
        "intelligence_version": "phase3.0_intelligence_v1",
        "intelligence_runtime_version": "phase3.0_intelligence_v1",
        "evidence_version": "evidence_v1",
        "score": score,
        "confidence_band": "HIGH" if score is not None and score >= 70 else "MODERATE",
        "disposition": disposition,
        "evidence": evidence,
        "score_components": components
        or [
            {
                "category": "STRUCTURE",
                "raw_evidence": ["STRUCTURE_ALIGNMENT"],
                "normalized_contribution": 10.0,
            },
            {"category": "ZONE_CONTEXT", "raw_evidence": [], "normalized_contribution": 0.0},
        ],
        "baseline_positive": baseline_positive,
        "outcome_status": outcome,
        "outcome_realized_r": realized_r,
        "outcome_timestamp": (timestamp + timedelta(minutes=5)).isoformat() if outcome else None,
        "execution_allowed": False,
    }


def test_evaluation_is_reproducible_with_fixed_generation_time() -> None:
    records = [record(2), record(1, outcome="SL_HIT", realized_r=-1.0, disposition="OBSERVE")]
    generated = BASE_TIME + timedelta(days=1)
    first = evaluate_records(records, generated_at=generated)
    second = evaluate_records(list(reversed(records)), generated_at=generated)
    assert first == second
    assert first["evaluation_version"] == EVALUATION_VERSION


def test_empty_dataset_reports_na_instead_of_zero() -> None:
    result = evaluate_records([], generated_at=BASE_TIME)
    assert result["sample_counts"]["observation_count"] == 0
    assert result["intelligence_metrics"]["precision"]["status"] == "N/A"
    assert result["intelligence_metrics"]["precision"]["value"] is None


def test_unresolved_outcomes_are_counted_but_excluded_from_rates() -> None:
    result = evaluate_records(
        [record(1, outcome="PENDING", realized_r=None)], generated_at=BASE_TIME
    )
    assert result["sample_counts"]["unresolved_count"] == 1
    assert result["intelligence_metrics"]["outcome_rate"]["status"] == "N/A"
    assert "resolved" in result["intelligence_metrics"]["outcome_rate"]["reason"]


def test_malformed_records_are_reported_explicitly() -> None:
    malformed = record(1)
    malformed["score"] = "not-a-score"
    result = evaluate_records([malformed], generated_at=BASE_TIME)
    assert result["sample_counts"]["observation_count"] == 0
    assert result["invalid_records"][0]["code"] == "MALFORMED_RECORD"


def test_missing_evidence_is_measured_as_component_missing() -> None:
    result = evaluate_records(
        [
            record(
                1,
                components=[
                    {"category": "STRUCTURE", "raw_evidence": [], "normalized_contribution": 0.0}
                ],
            )
        ],
        generated_at=BASE_TIME,
    )
    structure = result["component_metrics"]["STRUCTURE"]
    assert structure["missing_count"] == 1
    assert structure["missing_rate"]["value"] == 1.0


def test_persisted_matching_uses_forward_id_not_timestamp() -> None:
    intelligence = SimpleNamespace(
        id="intel-row",
        candidate_id="candidate-1",
        symbol="XAUUSDm",
        strategy="pair_zone_v1",
        strategy_version="pair_zone_v1",
        evidence_version="evidence_v1",
        detected_at=BASE_TIME,
        timeframe="M15/M5",
        direction="BUY",
        state="CONFIRMED",
        score=70.0,
        confidence_band="HIGH",
        alert_decision="ALERT",
        blockers_json=[],
        warnings_json=[],
        context_json={},
        evidence_json=[],
        score_components_json=[],
        forward_session_id="session-1",
        forward_signal_id=None,
        execution_allowed=False,
    )
    signal = SimpleNamespace(id="signal-with-different-time", decision="BUY")
    observations = observations_from_persisted_rows([intelligence], forward_signal_rows=[signal])
    assert observations[0].baseline_positive is None
    assert observations[0].outcome_status is None


def test_duplicate_observations_fail_closed() -> None:
    item = record(1)
    result = evaluate_records([item, item], generated_at=BASE_TIME)
    assert result["sample_counts"]["observation_count"] == 0
    assert any(issue["code"] == "DUPLICATE_OBSERVATION" for issue in result["invalid_records"])


def test_chronological_ordering_and_future_evidence_leakage_boundary() -> None:
    result = evaluate_records([record(2), record(1)], generated_at=BASE_TIME)
    assert result["data_coverage"]["earliest"] == (BASE_TIME + timedelta(hours=1)).isoformat()
    future = record(1, evidence_timestamp=BASE_TIME + timedelta(hours=2))
    rejected = evaluate_records([future], generated_at=BASE_TIME)
    assert rejected["invalid_records"][0]["code"] == "MALFORMED_RECORD"
    assert "future evidence" in rejected["invalid_records"][0]["detail"]


def test_confidence_bucket_boundaries_follow_current_contract() -> None:
    result = evaluate_records(
        [record(1, score=44.99), record(2, score=45), record(3, score=70)], generated_at=BASE_TIME
    )
    buckets = {
        row["bucket"]: row["observation_count"] for row in result["segmented_metrics"]["buckets"]
    }
    assert buckets == {"LOW": 1, "MODERATE": 1, "HIGH": 1, "MISSING": 0}


def test_manual_ob_rejection_confirmation_is_not_accepted() -> None:
    unsafe = record(1, manual_state="REJECTION_CONFIRMED")
    result = evaluate_records([unsafe], generated_at=BASE_TIME)
    assert result["sample_counts"]["observation_count"] == 0
    assert result["invalid_records"][0]["code"] == "SAFETY_VIOLATION"


def test_manual_ob_states_are_observational_only() -> None:
    result = evaluate_records([record(1, manual_state="APPROACHING_OB")], generated_at=BASE_TIME)
    assert result["manual_ob_metrics"]["rejection_confirmation_created"] is False
    assert (
        result["manual_ob_metrics"]["states"]["APPROACHING_OB"]["interpretation"]
        == "OBSERVATIONAL_ONLY"
    )


def test_evaluation_preserves_baseline_and_execution_safety() -> None:
    item = record(1, disposition="OBSERVE", baseline_positive=True)
    original = dict(item)
    result = evaluate_records([item], generated_at=BASE_TIME)
    assert item == original
    assert result["execution_allowed"] is False
    assert result["baseline_metrics"]["observation_count"] == 1


def test_temporal_split_is_chronological_and_does_not_tune_holdout() -> None:
    result = evaluate_records(
        [
            record(
                index,
                outcome="TP_HIT" if index % 2 else "SL_HIT",
                realized_r=1.0 if index % 2 else -1.0,
            )
            for index in range(12)
        ],
        generated_at=BASE_TIME,
    )
    split = result["temporal_split"]
    assert split["chronological"] is True
    assert split["status"] == "AVAILABLE"
    assert split["development"]["max_timestamp"] < split["holdout"]["min_timestamp"]
    assert split["thresholds_tuned_on_holdout"] is False


def test_calibration_is_na_because_score_is_not_a_probability() -> None:
    result = evaluate_records([record(1)], generated_at=BASE_TIME)
    assert result["segmented_metrics"]["calibration"]["status"] == "N/A"
    assert "not calibrated probabilities" in result["segmented_metrics"]["calibration"]["reason"]


def test_direct_observation_rejects_naive_timestamp() -> None:
    with pytest.raises(EvaluationInputError):
        observation_from_mapping(
            {
                "observation_id": "x",
                "symbol": "XAUUSDm",
                "timeframe": "M5",
                "timestamp": "2026-01-01T00:00:00",
            }
        )
