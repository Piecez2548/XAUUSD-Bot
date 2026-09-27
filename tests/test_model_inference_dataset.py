from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select

from models.model_inference_dataset import (
    DATASET_V2_CONTRACT_VERSION,
    DatasetManifestV2,
    DatasetRowV1,
    DatasetRowV2,
    canonical_dataset_row_fingerprint_payload,
    dataset_row_fingerprint,
)
from persistence.database import Database
from persistence.orm import (
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    PairZoneEvaluationRecord,
    StrategyIntelligenceRecord,
)
from services.model_inference_dataset import (
    MAX_PAGE_SIZE,
    DatasetArtifactDependencyError,
    ModelInferenceDatasetBuilder,
    write_dataset_artifact,
)


def _context(*, future: datetime | None = None, complete: bool = True) -> dict:
    context = {
        "symbol": "XAUUSD",
        "as_of": datetime(2026, 9, 25, 12, 0, tzinfo=UTC).isoformat(),
        "version": "market_context_v1",
        "data_status": "READY" if complete else "INSUFFICIENT_DATA",
        "m15_trend": "BULLISH",
        "m5_trend": "BULLISH",
        "m15_structure": "HH_HL",
        "m5_structure": "HH_HL",
        "m15_atr": 2.0,
        "m5_atr": 0.7,
        "m15_displacement": 1.1,
        "m5_displacement": 0.9,
        "m5_relative_volatility": 1.2,
        "session": "LONDON",
        "m15_swing_points": [
            {"kind": "SWING_HIGH", "pivot_timestamp": "2026-09-25T11:00:00+00:00"},
            {"kind": "SWING_HIGH", "pivot_timestamp": "2026-09-25T11:15:00+00:00"},
            {"kind": "SWING_LOW", "pivot_timestamp": "2026-09-25T11:30:00+00:00"},
            {"kind": "SWING_LOW", "pivot_timestamp": "2026-09-25T11:45:00+00:00"},
        ],
        "m5_swing_points": [
            {"kind": "SWING_HIGH", "pivot_timestamp": "2026-09-25T11:50:00+00:00"},
            {"kind": "SWING_LOW", "pivot_timestamp": "2026-09-25T11:55:00+00:00"},
        ],
        "latest_m15_timestamp": "2026-09-25T11:45:00+00:00",
        "latest_m5_timestamp": "2026-09-25T11:55:00+00:00",
    }
    if future is not None:
        context["latest_m15_timestamp"] = future.isoformat()
    return context


def _fingerprint_payload() -> dict:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC).isoformat()
    return {
        "dataset_contract_version": "model_inference_dataset_v1",
        "feature_contract_version": "model_training_features_v1",
        "candidate_id": "candidate-fingerprint",
        "symbol": "XAUUSD",
        "candidate_timestamp": now,
        "causal_cutoff_timestamp": now,
        "state": "OUTCOME_COMPLETED",
        "training_eligibility": "TRAINABLE",
        "reason_codes": ("SECOND", "FIRST"),
        "classification": "SUPPORTIVE",
        "outcome_label": "TP",
        "features": {
            "unicode": "café",
            "nested": {"flag": True, "count": 3, "ratio": 1.25, "empty": None},
        },
        "source_provenance": {"strategy": "exact_pair", "candidate_id": "source-1"},
        "source_timestamps": {"candidate_detected_at": now},
        "row_identity": "a" * 64,
    }


def test_row_fingerprint_canonical_contract_and_self_reference_safety():
    payload = _fingerprint_payload()
    reordered = {key: payload[key] for key in reversed(tuple(payload))}
    reordered["features"] = {
        "nested": {"empty": None, "ratio": 1.25, "count": 3, "flag": True},
        "unicode": "café",
    }
    reordered["source_provenance"] = {
        "candidate_id": "source-1",
        "strategy": "exact_pair",
    }

    assert dataset_row_fingerprint(payload) == dataset_row_fingerprint(reordered)
    assert dataset_row_fingerprint(payload) == dataset_row_fingerprint(
        {
            **payload,
            "candidate_timestamp": payload["candidate_timestamp"].replace(
                "+00:00", "Z"
            ),
            "causal_cutoff_timestamp": payload["causal_cutoff_timestamp"].replace(
                "+00:00", "Z"
            ),
            "source_timestamps": {"candidate_detected_at": "2026-09-25T12:00:00Z"},
        }
    )
    assert dataset_row_fingerprint(
        {**payload, "row_fingerprint": "0" * 64}
    ) == dataset_row_fingerprint({**payload, "row_fingerprint": "f" * 64})

    canonical = canonical_dataset_row_fingerprint_payload(payload)
    serialized = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    assert hashlib.sha256(serialized).hexdigest() == dataset_row_fingerprint(payload)
    assert "row_fingerprint" not in canonical


@pytest.mark.parametrize(
    "field, value",
    [
        ("candidate_id", "candidate-other"),
        ("causal_cutoff_timestamp", "2026-09-25T12:15:00+00:00"),
        ("outcome_label", "SL"),
        ("features", {"unicode": "café", "nested": {"flag": False}}),
        ("source_provenance", {"strategy": "other", "candidate_id": "source-1"}),
    ],
)
def test_semantic_row_changes_change_fingerprint(field, value):
    payload = _fingerprint_payload()
    changed = {**payload, field: value}
    assert dataset_row_fingerprint(changed) != dataset_row_fingerprint(payload)


def test_nonsemantic_metadata_is_rejected_and_cannot_affect_fingerprint():
    payload = _fingerprint_payload()
    with pytest.raises(ValueError):
        dataset_row_fingerprint({**payload, "artifact_path": r"C:\private\dataset.jsonl"})
    with pytest.raises(ValueError):
        dataset_row_fingerprint({**payload, "build_timestamp": "2026-09-27T00:00:00Z"})


def test_fingerprint_contract_rejects_missing_or_extra_semantic_fields():
    payload = _fingerprint_payload()
    missing = dict(payload)
    missing.pop("features")
    with pytest.raises(ValueError):
        dataset_row_fingerprint(missing)
    with pytest.raises(ValueError):
        dataset_row_fingerprint({**payload, "unexpected": True})


def test_fingerprint_representation_is_lowercase_sha256_and_row_validation_is_strict():
    payload = _fingerprint_payload()
    fingerprint = dataset_row_fingerprint(payload)
    assert len(fingerprint) == 64
    assert fingerprint == fingerprint.lower()
    with pytest.raises(ValidationError):
        DatasetRowV1.model_validate({**payload, "row_fingerprint": fingerprint.upper()})


def _source(
    detected_at: datetime,
    *,
    candidate_id: str,
    context: dict | None = None,
    session_id: str | None = None,
    signal_id: str | None = None,
    trade_id: str | None = None,
    pair_zone_id: str | None = None,
    v2: bool = False,
    observation_available_at: datetime | None = None,
    pair_zone_decision_evidence_id: str | None = None,
) -> StrategyIntelligenceRecord:
    return StrategyIntelligenceRecord(
        candidate_id=candidate_id,
        symbol="XAUUSD",
        strategy="exact_pair",
        strategy_version="pair-v1",
        intelligence_version="phase3.0_intelligence_v1",
        intelligence_runtime_version="phase3.0_intelligence_v1",
        evidence_version="evidence_v1",
        detected_at=detected_at,
        timeframe="M15",
        direction="BUY",
        state="CONFIRMED",
        score=80.0,
        confidence_band="HIGH",
        alert_decision="ALERT",
        blockers_json=[],
        warnings_json=[],
        context_json=context or _context(),
        evidence_json=[],
        score_components_json=[],
        source="test",
        pair_zone_event_id=pair_zone_id,
        provenance_contract_version=(
            "strategy_intelligence_provenance_v2" if v2 else None
        ),
        source_timeframe="M15" if v2 else None,
        confirmation_timeframe="M5" if v2 else None,
        observation_available_at=observation_available_at if v2 else None,
        pair_zone_decision_evidence_id=(
            pair_zone_decision_evidence_id if v2 else None
        ),
        forward_session_id=session_id,
        forward_signal_id=signal_id,
        forward_trade_id=trade_id,
        execution_allowed=False,
    )


def _forward_rows(cutoff: datetime, state: str = "TP", *, v2: bool = False) -> tuple[
    ForwardValidationSessionRecord, ForwardSignalRecord, ForwardTradeRecord
]:
    session = ForwardValidationSessionRecord(
        id="session-1",
        session_id="session-1",
        strategy_id="exact_pair",
        strategy_version="pair-v1",
        strategy_config_hash="a" * 64,
        started_at=cutoff - timedelta(hours=1),
        source_identity="fixture",
        symbol="XAUUSD",
        timeframes_json=["M5", "M15"],
        rr=2.0,
        cost_policy_json={},
        status="RUNNING",
        execution_allowed=False,
    )
    signal = ForwardSignalRecord(
        id="signal-1",
        signal_id="signal-1",
        session_id="session-1",
        timestamp=cutoff,
        symbol="XAUUSD",
        decision="BUY",
        zone_id="zone-1",
        entry_price=2000.0,
        stop_loss=1990.0,
        risk_distance=10.0,
        rr=2.0,
        take_profit=2020.0,
        pair_first_timestamp=cutoff - timedelta(minutes=30),
        pair_second_timestamp=cutoff - timedelta(minutes=15),
        h1_context_json={"timestamp": (cutoff - timedelta(hours=1)).isoformat()},
        confirmation_candle_json={"timestamp": cutoff.isoformat()},
        market_observation_json={"timestamp": cutoff.isoformat()},
        strategy_hash="a" * 64,
        created_at=cutoff,
        provenance_contract_version=(
            "strategy_intelligence_provenance_v2" if v2 else None
        ),
        source_timeframe="M15" if v2 else None,
        confirmation_timeframe="M5" if v2 else None,
        signal_decision_at=cutoff if v2 else None,
        decision_available_at=cutoff + timedelta(minutes=5) if v2 else None,
        pair_zone_decision_evidence_id="event-1" if v2 else None,
        execution_allowed=False,
    )
    trade = ForwardTradeRecord(
        id="trade-1",
        trade_id="trade-1",
        session_id="session-1",
        signal_id="signal-1",
        timestamp=cutoff,
        side="BUY",
        state=state,
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit=2020.0,
        risk_distance=10.0,
        terminal_timestamp=None if state == "OPEN" else cutoff + timedelta(minutes=30),
        outcome_available_at=None if state == "OPEN" else cutoff + timedelta(minutes=35),
        mark_price=None,
        gross_r=None if state in {"OPEN", "AMBIGUOUS"} else 2.0,
        net_r=None if state in {"OPEN", "AMBIGUOUS"} else 1.8,
        bars_held=0,
        minutes_held=None,
        mfe_price=None,
        mae_price=None,
        mfe_r=None,
        mae_r=None,
        spread_points=10.0,
        spread_observation="OBSERVED",
        entry_slippage_points=0.0,
        exit_slippage_points=0.0,
        commission_r=0.0,
        total_cost_r=0.2,
        evaluated_at=None if state == "OPEN" else cutoff + timedelta(minutes=31),
        reason_code="fixture",
        execution_allowed=False,
    )
    return session, signal, trade


@pytest.fixture
def database(tmp_path):
    database = Database.for_test(f"sqlite:///{(tmp_path / 'dataset.db').as_posix()}")
    database.create_test_schema()
    yield database
    database.dispose()


def _insert(database: Database, *rows) -> None:
    with database.session() as session:
        # Keep the fixture insertion order explicit because this schema uses
        # foreign keys while SQLAlchemy has no relationships for these rows.
        for row in rows:
            session.add(row)
            session.flush()


def test_pre_signal_observation_is_not_a_losing_label(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(database, _source(cutoff, candidate_id="pre-signal"))

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.state == "PRE_SIGNAL_OBSERVATION"
    assert row.outcome_label == "NOT_ELIGIBLE"
    assert row.training_eligibility == "NON_TRAINABLE"


def test_v2_uses_immutable_signal_provenance_not_mutable_latest_pair_zone(database):
    detected = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    available = detected + timedelta(minutes=5)
    session, signal, trade = _forward_rows(detected, "TP", v2=True)
    source = _source(
        detected,
        candidate_id="v2-trainable",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
        v2=True,
        observation_available_at=available,
        pair_zone_decision_evidence_id="event-1",
    )
    mutable_latest = PairZoneEvaluationRecord(
        forward_session_id=session.id,
        runtime_generation_id="later-generation",
        evaluation_at=detected + timedelta(hours=4),
        evaluated_m5_timestamp=detected + timedelta(hours=4),
        evaluated_m15_timestamp=detected + timedelta(hours=4),
        strategy_id="exact_pair",
        strategy_version="pair-v1",
        config_hash="a" * 64,
        state="ACTIVE_ZONE",
        reason="later-observation",
        direction="BUY",
        zone_id="different-latest-zone",
        zone_lower=1.0,
        zone_upper=2.0,
    )
    _insert(database, session, signal, trade, source, mutable_latest)

    row = next(
        ModelInferenceDatasetBuilder(
            database,
            session_aware_v2_accepted=True,
            dataset_contract_version=DATASET_V2_CONTRACT_VERSION,
        ).iter_rows()
    )

    assert isinstance(row, DatasetRowV2)
    assert row.dataset_contract_version == DATASET_V2_CONTRACT_VERSION
    assert row.training_eligibility == "TRAINABLE"
    assert row.causal_cutoff_timestamp == available
    assert row.pair_zone_decision_evidence_id == "event-1"
    assert "POST_DECISION_PAIR_ZONE_EVIDENCE" not in row.reason_codes
    assert "FUTURE_PAIR_ZONE_CANDLE" not in row.reason_codes


def _v2_row(database, *, state: str = "TP", terminal_offset: int = 30,
            outcome_offset: int = 35, evaluated_at: datetime | None = None):
    detected = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    available = detected + timedelta(minutes=5)
    session, signal, trade = _forward_rows(detected, state, v2=True)
    trade.terminal_timestamp = detected + timedelta(minutes=terminal_offset)
    trade.outcome_available_at = detected + timedelta(minutes=outcome_offset)
    trade.evaluated_at = evaluated_at or detected + timedelta(minutes=31)
    source = _source(
        detected,
        candidate_id=f"v2-{terminal_offset}-{outcome_offset}",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
        v2=True,
        observation_available_at=available,
        pair_zone_decision_evidence_id="event-1",
    )
    _insert(database, session, signal, trade, source)
    return next(
        ModelInferenceDatasetBuilder(
            database,
            session_aware_v2_accepted=True,
            dataset_contract_version=DATASET_V2_CONTRACT_VERSION,
        ).iter_rows()
    )


def test_v2_accepts_first_future_candle_when_outcome_is_available_after_decision(
    database,
):
    row = _v2_row(database, terminal_offset=5, outcome_offset=10)
    assert row.training_eligibility == "TRAINABLE"
    assert row.outcome_label == "TP"


def test_v2_accepts_first_future_sl_candle_when_outcome_is_available_after_decision(
    database,
):
    row = _v2_row(database, state="SL", terminal_offset=5, outcome_offset=10)
    assert row.training_eligibility == "TRAINABLE"
    assert row.outcome_label == "SL"


def test_v2_rejects_same_candle_outcome_even_with_later_availability(database):
    row = _v2_row(database, terminal_offset=0, outcome_offset=5)
    assert row.training_eligibility == "UNRESOLVED"
    assert "INCOMPLETE_OUTCOME_PROVENANCE" in row.reason_codes


def test_v2_evaluated_at_is_noncausal_processing_metadata(database):
    row = _v2_row(database, evaluated_at=datetime(2026, 9, 25, 11, 59, tzinfo=UTC))
    assert row.training_eligibility == "TRAINABLE"


def test_v2_signal_created_at_does_not_change_fingerprint(database):
    first = _v2_row(database)
    with database.session() as session:
        signal = session.get(ForwardSignalRecord, "signal-1")
        assert signal is not None
        signal.created_at = signal.created_at + timedelta(days=1)
    second = next(
        ModelInferenceDatasetBuilder(
            database,
            session_aware_v2_accepted=True,
            dataset_contract_version=DATASET_V2_CONTRACT_VERSION,
        ).iter_rows()
    )
    assert first.row_fingerprint == second.row_fingerprint
    assert "signal_created_at" not in second.source_timestamps


def test_v2_does_not_fallback_to_legacy_pair_zone_event_id(database):
    detected = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    available = detected + timedelta(minutes=5)
    session, signal, trade = _forward_rows(detected, "TP", v2=True)
    signal.pair_zone_decision_evidence_id = None
    source = _source(
        detected,
        candidate_id="missing-v2-evidence",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
        v2=True,
        observation_available_at=available,
        pair_zone_decision_evidence_id=None,
    )
    _insert(database, session, signal, trade, source)
    row = next(
        ModelInferenceDatasetBuilder(
            database,
            session_aware_v2_accepted=True,
            dataset_contract_version=DATASET_V2_CONTRACT_VERSION,
        ).iter_rows()
    )
    assert row.training_eligibility == "UNRESOLVED"
    assert "FORWARD_PROVENANCE_MISMATCH" in row.reason_codes


def test_v2_builder_keeps_legacy_rows_unresolved_without_fabricated_provenance(database):
    detected = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(database, _source(detected, candidate_id="legacy-row"))

    row = next(
        ModelInferenceDatasetBuilder(
            database, dataset_contract_version=DATASET_V2_CONTRACT_VERSION
        ).iter_rows()
    )

    assert isinstance(row, DatasetRowV1)
    assert row.dataset_contract_version == "model_inference_dataset_v1"
    assert row.training_eligibility == "UNRESOLVED"
    assert row.outcome_label == "UNRESOLVED"
    assert "LEGACY_PROVENANCE_CONTRACT" in row.reason_codes
    assert "observation_available_at" not in DatasetRowV1.model_fields


def test_v2_rejects_signal_evidence_after_decision_boundary(database):
    detected = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    available = detected + timedelta(minutes=5)
    session, signal, trade = _forward_rows(detected, "TP", v2=True)
    signal.confirmation_candle_json = {
        "timestamp": (available + timedelta(minutes=1)).isoformat()
    }
    source = _source(
        detected,
        candidate_id="v2-future-confirmation",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="event-1",
        v2=True,
        observation_available_at=available,
    )
    _insert(database, session, signal, trade, source)

    row = next(
        ModelInferenceDatasetBuilder(
            database,
            session_aware_v2_accepted=True,
            dataset_contract_version=DATASET_V2_CONTRACT_VERSION,
        ).iter_rows()
    )

    assert row.training_eligibility == "UNRESOLVED"
    assert "POST_DECISION_SIGNAL_EVIDENCE" in row.reason_codes


def test_v2_artifact_has_explicit_versioned_manifest(database, tmp_path):
    detected = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    session, signal, trade = _forward_rows(detected, "TP", v2=True)
    source = _source(
        detected,
        candidate_id="v2-artifact",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="event-1",
        v2=True,
        observation_available_at=detected + timedelta(minutes=5),
    )
    _insert(database, session, signal, trade, source)

    manifest, artifact = write_dataset_artifact(
        ModelInferenceDatasetBuilder(
            database,
            session_aware_v2_accepted=True,
            dataset_contract_version=DATASET_V2_CONTRACT_VERSION,
        ),
        tmp_path,
    )

    assert isinstance(manifest, DatasetManifestV2)
    assert manifest.dataset_contract_version == DATASET_V2_CONTRACT_VERSION
    assert artifact.name == "model_inference_dataset_v2.jsonl"
    assert (tmp_path / "model_inference_dataset_v2.manifest.json").exists()


def test_signal_without_trade_is_signal_eligible_not_a_loss(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    session, signal, _trade = _forward_rows(cutoff, "OPEN")
    source = _source(
        cutoff,
        candidate_id="signal-eligible",
        session_id=session.id,
        signal_id=signal.id,
        pair_zone_id="zone-1",
    )
    _insert(database, session, signal, source)

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.state == "SIGNAL_ELIGIBLE"
    assert row.outcome_label == "SIGNAL_ELIGIBLE"
    assert row.training_eligibility == "NON_TRAINABLE"


@pytest.mark.parametrize("state", ["OPEN", "EXPIRED", "AMBIGUOUS"])
def test_non_terminal_or_ambiguous_outcomes_never_train(database, state):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    session, signal, trade = _forward_rows(cutoff, state)
    source = _source(
        cutoff,
        candidate_id=f"candidate-{state}",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
    )
    _insert(database, session, signal, trade, source)

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.training_eligibility == "NON_TRAINABLE"
    assert row.outcome_label in {"OPEN/PENDING", "EXPIRED", "AMBIGUOUS"}
    assert row.outcome_label not in {"TP", "SL"} or state == "OPEN"


@pytest.mark.parametrize("terminal_state", ["TP", "SL"])
def test_terminal_outcome_is_trainable_only_when_v2_is_explicitly_accepted(
    database, terminal_state
):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    session, signal, trade = _forward_rows(cutoff, terminal_state)
    source = _source(
        cutoff,
        candidate_id=f"candidate-{terminal_state.lower()}",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
    )
    _insert(database, session, signal, trade, source)

    rejected = next(ModelInferenceDatasetBuilder(database).iter_rows())
    accepted = next(
        ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows()
    )

    assert rejected.outcome_label == terminal_state
    assert rejected.training_eligibility == "NON_TRAINABLE"
    assert "SESSION_AWARE_V2_NOT_ACCEPTED" in rejected.reason_codes
    assert accepted.training_eligibility == "TRAINABLE"
    assert "gross_r" not in accepted.features
    assert "mfe_r" not in accepted.features
    assert "mae_r" not in accepted.features


def test_future_causal_evidence_is_unresolved(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    future = cutoff + timedelta(minutes=15)
    _insert(
        database,
        _source(cutoff, candidate_id="future", context=_context(future=future)),
    )

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.state == "PRE_SIGNAL_OBSERVATION"
    assert row.training_eligibility == "UNRESOLVED"
    assert "FUTURE_FEATURE_EVIDENCE" in row.reason_codes


def test_future_risk_payload_and_unexpected_fields_cannot_become_features(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    context = _context()
    context["future_risk_snapshot"] = {
        "timestamp": (cutoff + timedelta(minutes=1)).isoformat(),
        "open_risk_percent": 99.0,
    }
    context["telegram_token"] = "secret-must-not-export"
    _insert(database, _source(cutoff, candidate_id="future-risk", context=context))

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.training_eligibility == "UNRESOLVED"
    assert "future_risk_snapshot" not in row.features
    assert "telegram_token" not in row.features
    assert "secret-must-not-export" not in row.model_dump_json()


def test_post_decision_signal_evidence_is_unresolved(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    future = cutoff + timedelta(minutes=15)
    session, signal, trade = _forward_rows(cutoff, "TP")
    signal.market_observation_json = {"timestamp": future.isoformat()}
    source = _source(
        cutoff,
        candidate_id="post-decision-signal",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
    )
    _insert(database, session, signal, trade, source)

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.training_eligibility == "UNRESOLVED"
    assert "POST_DECISION_SIGNAL_EVIDENCE" in row.reason_codes


def test_future_pair_zone_and_symbol_mismatch_are_unresolved(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    session, signal, trade = _forward_rows(cutoff, "TP")
    pair_zone = PairZoneEvaluationRecord(
        forward_session_id=session.id,
        runtime_generation_id="generation-1",
        evaluation_at=cutoff + timedelta(minutes=15),
        evaluated_m5_timestamp=cutoff + timedelta(minutes=5),
        evaluated_m15_timestamp=cutoff + timedelta(minutes=15),
        strategy_id="exact_pair",
        strategy_version="pair-v1",
        config_hash="a" * 64,
        state="ACTIVE_ZONE",
        reason="fixture",
        direction="BUY",
        zone_id="zone-1",
        zone_lower=1990.0,
        zone_upper=2000.0,
    )
    source = _source(
        cutoff,
        candidate_id="future-pair-zone",
        session_id=session.id,
        signal_id=signal.id,
        trade_id=trade.id,
        pair_zone_id="zone-1",
    )
    session.symbol = "XAUUSDm"
    _insert(database, session, signal, trade, pair_zone, source)

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.training_eligibility == "UNRESOLVED"
    assert "POST_DECISION_PAIR_ZONE_EVIDENCE" in row.reason_codes
    assert "FORWARD_PROVENANCE_MISMATCH" in row.reason_codes


def test_unordered_swing_timestamps_are_unresolved(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    context = _context()
    context["m15_swing_points"][0], context["m15_swing_points"][1] = (
        context["m15_swing_points"][1],
        context["m15_swing_points"][0],
    )
    _insert(database, _source(cutoff, candidate_id="unordered", context=context))

    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    assert row.training_eligibility == "UNRESOLVED"
    assert "UNORDERED_SOURCE_TIMESTAMPS" in row.reason_codes


def test_duplicate_candidate_and_changed_evidence_fail_closed(database, monkeypatch):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(database, _source(cutoff, candidate_id="duplicate"))
    with database.session() as session:
        source = session.scalar(select(StrategyIntelligenceRecord))
    assert source is not None
    joined = (source, None, None, None, None, None)
    duplicate_builder = ModelInferenceDatasetBuilder(
        database, page_size=2, session_aware_v2_accepted=True
    )
    monkeypatch.setattr(duplicate_builder, "_page", lambda _cursor: [joined, joined])
    duplicate_rows = list(duplicate_builder.iter_rows())
    assert duplicate_rows[1].training_eligibility == "UNRESOLVED"
    assert "DUPLICATE_CANDIDATE_IDENTITY" in duplicate_rows[1].reason_codes

    first = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())
    with database.session() as session:
        current = session.scalar(select(StrategyIntelligenceRecord))
        assert current is not None
        current.context_json = {**current.context_json, "m15_atr": 3.0}
    second = next(
        ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows()
    )
    assert first.candidate_id == second.candidate_id
    assert first.row_fingerprint != second.row_fingerprint


def test_keyset_pages_are_ordered_and_read_only(database):
    start = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(
        database,
        *(
            _source(start + timedelta(minutes=index), candidate_id=f"candidate-{index}")
            for index in range(3)
        ),
    )
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(str(statement).upper())

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        rows = list(
            ModelInferenceDatasetBuilder(
                database, page_size=1, session_aware_v2_accepted=True
            ).iter_rows()
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)

    assert [row.candidate_id for row in rows] == [
        "candidate-0",
        "candidate-1",
        "candidate-2",
    ]
    assert not any(
        statement.lstrip().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))
        for statement in statements
    )
    with database.session() as session:
        assert len(session.scalars(select(StrategyIntelligenceRecord)).all()) == 3


def test_timestamp_ties_use_id_cursor_and_page_limit_is_bounded(database):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(
        database,
        *(_source(cutoff, candidate_id=f"tie-{index}") for index in range(4)),
    )
    with database.session() as session:
        expected = [
            row.candidate_id
            for row in session.scalars(
                select(StrategyIntelligenceRecord).order_by(
                    StrategyIntelligenceRecord.detected_at,
                    StrategyIntelligenceRecord.id,
                )
            ).all()
        ]
    actual = [
        row.candidate_id
        for row in ModelInferenceDatasetBuilder(
            database, page_size=1, session_aware_v2_accepted=True
        ).iter_rows()
    ]
    assert actual == expected
    with pytest.raises(ValueError):
        ModelInferenceDatasetBuilder(database, page_size=MAX_PAGE_SIZE + 1)


def test_jsonl_manifest_is_deterministic_and_parquet_is_explicit(database, tmp_path):
    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(database, _source(cutoff, candidate_id="artifact"))
    builder = ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True)

    manifest, artifact = write_dataset_artifact(builder, tmp_path / "one")
    manifest_two, _ = write_dataset_artifact(
        ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True), tmp_path / "two"
    )

    assert artifact.suffix == ".jsonl"
    assert manifest.dataset_hash == manifest_two.dataset_hash
    assert manifest.artifact_sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        write_dataset_artifact(
            ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True), tmp_path / "one"
        )
    with pytest.raises(DatasetArtifactDependencyError):
        write_dataset_artifact(
            ModelInferenceDatasetBuilder(database), tmp_path / "parquet", artifact_format="parquet"
        )


def test_empty_and_interrupted_builds_do_not_claim_completion(database, tmp_path):
    empty_dir = tmp_path / "empty"
    manifest, artifact = write_dataset_artifact(
        ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True), empty_dir
    )
    assert manifest.row_count == 0
    assert artifact.exists()
    assert (empty_dir / "model_inference_dataset_v1.manifest.json").exists()

    cutoff = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    _insert(database, _source(cutoff, candidate_id="interrupt"))
    row = next(ModelInferenceDatasetBuilder(database, session_aware_v2_accepted=True).iter_rows())

    class InterruptedBuilder:
        page_size = 1
        session_aware_v2_accepted = True

        def iter_rows(self):
            yield row
            raise RuntimeError("fixture interruption")

    interrupted_dir = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="fixture interruption"):
        write_dataset_artifact(InterruptedBuilder(), interrupted_dir)
    assert not (interrupted_dir / "model_inference_dataset_v1.jsonl").exists()
    assert not (interrupted_dir / "model_inference_dataset_v1.manifest.json").exists()
