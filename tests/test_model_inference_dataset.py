from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

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


def _source(
    detected_at: datetime,
    *,
    candidate_id: str,
    context: dict | None = None,
    session_id: str | None = None,
    signal_id: str | None = None,
    trade_id: str | None = None,
    pair_zone_id: str | None = None,
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
        forward_session_id=session_id,
        forward_signal_id=signal_id,
        forward_trade_id=trade_id,
        execution_allowed=False,
    )


def _forward_rows(cutoff: datetime, state: str = "TP") -> tuple[
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
