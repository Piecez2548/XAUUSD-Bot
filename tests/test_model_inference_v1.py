from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select

from alembic import command
from config.settings import Settings
from models.intelligence import AlertDecision, CandidateState, Direction
from models.model_inference import (
    AdvisoryClassification,
    ModelInferenceInputV1,
)
from persistence.database import Database
from persistence.orm import (
    ForwardValidationSessionRecord,
    ModelInferenceEvaluationRecord,
    StrategyIntelligenceRecord,
)
from services.model_inference import (
    INFERENCE_VERSION,
    InferenceIdempotencyConflict,
    build_inference_input,
    evaluate_advisory,
    persist_advisory_evaluation,
)

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'model-inference.db').as_posix()}")
    database.create_schema()
    return database


def _seed_evidence(database: Database, *, candidate_id: str = "candidate-v1") -> str:
    with database.session() as session:
        forward_session = ForwardValidationSessionRecord(
            session_id="forward-model-v1",
            strategy_id="pair_zone_v1",
            strategy_version="pair-zone-1",
            strategy_config_hash="a" * 64,
            started_at=NOW - timedelta(hours=1),
            source_identity="read-only-test-fixture",
            symbol="XAUUSD",
            timeframes_json=["M5", "M15", "H1"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        )
        session.add(forward_session)
        session.flush()
        candidate = StrategyIntelligenceRecord(
            candidate_id=candidate_id,
            symbol="XAUUSD",
            strategy="pair_zone_v1",
            strategy_version="pair-zone-1",
            intelligence_version="phase3.0_intelligence_v1",
            intelligence_runtime_version="phase3.0_intelligence_v1",
            evidence_version="evidence_v1",
            detected_at=NOW,
            timeframe="M15/M5",
            direction="BUY",
            state="CONFIRMED",
            score=80.0,
            confidence_band="HIGH",
            alert_decision="ALERT",
            blockers_json=[],
            warnings_json=[],
            context_json={"data_status": "READY", "unrelated_secret_fixture": "must-not-copy"},
            evidence_json=[{"rule_id": "test-rule"}],
            score_components_json=[],
            source="ExactPairAdapter",
            pair_zone_event_id="pair-zone-event-v1",
            forward_session_id=forward_session.id,
            execution_allowed=False,
        )
        session.add(candidate)
        session.flush()
        return forward_session.id


def _input(**overrides) -> ModelInferenceInputV1:
    values = {
        "source_intelligence_record_id": "intelligence-row-id",
        "source_candidate_id": "candidate-v1",
        "forward_session_id": "forward-session-id",
        "forward_signal_id": "forward-signal-id",
        "pair_zone_event_id": "zone-event-id",
        "strategy_config_hash": "a" * 64,
        "strategy_version": "pair-zone-1",
        "evidence_version": "evidence_v1",
        "source_detected_at": NOW,
        "source_state": CandidateState.CONFIRMED,
        "source_direction": Direction.BUY,
        "source_alert_decision": AlertDecision.ALERT,
        "source_blocker_count": 0,
    }
    values.update(overrides)
    return ModelInferenceInputV1(**values)


def test_input_is_immutable_minimal_and_carries_versioned_provenance(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _seed_evidence(database)
    try:
        with database.session() as session:
            source = session.scalar(
                select(StrategyIntelligenceRecord).where(
                    StrategyIntelligenceRecord.candidate_id == "candidate-v1"
                )
            )
            assert source is not None
            inference_input = build_inference_input(
                source,
                forward_session_id=source.forward_session_id,
                strategy_config_hash="a" * 64,
            )
        assert inference_input.source_candidate_id == "candidate-v1"
        assert inference_input.forward_session_id is not None
        assert inference_input.strategy_config_hash == "a" * 64
        assert not hasattr(inference_input, "account_equity")
        with pytest.raises(ValueError):
            inference_input.source_state = CandidateState.INVALIDATED
    finally:
        database.dispose()


def test_deterministic_versioned_result_and_executable_instruction_absent() -> None:
    source = _input()
    first = evaluate_advisory(source, evaluated_at=NOW)
    second = evaluate_advisory(source, evaluated_at=NOW)
    assert first == second
    assert first.inference_version == INFERENCE_VERSION == "deterministic_advisory_v1"
    assert first.advisory_classification == AdvisoryClassification.SUPPORTIVE
    assert first.reason_codes == ("SOURCE_ALERT_EVIDENCE",)
    assert first.execution_allowed is False
    assert not hasattr(first, "order")
    assert not hasattr(first, "direction")


def test_output_reason_codes_are_nonempty_and_bounded() -> None:
    output = evaluate_advisory(_input(), evaluated_at=NOW)
    values = output.model_dump()
    values["reason_codes"] = ("R" * 65,)
    with pytest.raises(ValueError):
        type(output)(**values)
    values["reason_codes"] = tuple(f"REASON_{index}" for index in range(9))
    with pytest.raises(ValueError):
        type(output)(**values)
    values["reason_codes"] = ()
    with pytest.raises(ValueError):
        type(output)(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"source_alert_decision": AlertDecision.BLOCK}, AdvisoryClassification.CAUTION),
        ({"source_blocker_count": 1}, AdvisoryClassification.CAUTION),
        ({"source_state": CandidateState.INVALIDATED}, AdvisoryClassification.CAUTION),
        ({"source_alert_decision": AlertDecision.OBSERVE}, AdvisoryClassification.OBSERVATION_ONLY),
    ],
)
def test_advisory_classification_has_explicit_non_probability_semantics(
    overrides: dict[str, object], expected: AdvisoryClassification
) -> None:
    result = evaluate_advisory(_input(**overrides), evaluated_at=NOW)
    assert result.advisory_classification == expected
    assert "score" not in result.model_dump()
    assert "confidence" not in result.model_dump()


def test_optional_provenance_is_allowed_but_malformed_evidence_fails_closed() -> None:
    optional_missing = _input(forward_session_id=None, forward_signal_id=None,
                              pair_zone_event_id=None, strategy_config_hash=None)
    assert evaluate_advisory(optional_missing, evaluated_at=NOW).execution_allowed is False
    with pytest.raises(ValueError):
        _input(forward_session_id=None, forward_signal_id="orphan-signal")
    with pytest.raises(ValueError):
        _input(source_state="UNRECOGNIZED_STATE")
    with pytest.raises(ValueError):
        _input(strategy_config_hash="not-a-hash")


def test_persistence_is_separate_versioned_and_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id = _seed_evidence(database)
    try:
        first = persist_advisory_evaluation(
            database, candidate_id="candidate-v1", forward_session_id=session_id
        )
        second = persist_advisory_evaluation(
            database, candidate_id="candidate-v1", forward_session_id=session_id
        )
        assert first.id == second.id
        assert first.evaluation_key == second.evaluation_key
        assert first.inference_version == INFERENCE_VERSION
        assert first.advisory_classification == "SUPPORTIVE"
        assert first.execution_allowed is False
        assert first.input_json["source_candidate_id"] == "candidate-v1"
        assert first.strategy_config_hash == "a" * 64
        assert "must-not-copy" not in str(first.input_json)
        with database.session() as session:
            assert len(list(session.scalars(select(ModelInferenceEvaluationRecord)))) == 1
            source = session.scalar(
                select(StrategyIntelligenceRecord).where(
                    StrategyIntelligenceRecord.candidate_id == "candidate-v1"
                )
            )
            assert source is not None
            assert source.state == "CONFIRMED"
            assert source.forward_signal_id is None
    finally:
        database.dispose()


def test_same_identity_with_changed_evidence_is_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id = _seed_evidence(database)
    try:
        persist_advisory_evaluation(
            database, candidate_id="candidate-v1", forward_session_id=session_id
        )
        with database.session() as session:
            source = session.scalar(
                select(StrategyIntelligenceRecord).where(
                    StrategyIntelligenceRecord.candidate_id == "candidate-v1"
                )
            )
            assert source is not None
            source.alert_decision = "OBSERVE"
        with pytest.raises(InferenceIdempotencyConflict):
            persist_advisory_evaluation(
                database, candidate_id="candidate-v1", forward_session_id=session_id
            )
    finally:
        database.dispose()


def test_model_service_has_no_demo_or_broker_write_dependency() -> None:
    source = Path("services/model_inference.py").read_text(encoding="utf-8")
    assert "DemoExecutionService" not in source
    assert "order_send" not in source
    assert "mt5" not in source.lower()
    result = evaluate_advisory(_input(), evaluated_at=NOW)
    assert result.execution_allowed is False


@pytest.mark.asyncio
async def test_inference_failure_isolated_from_forward_signal_and_demo_handler(
    tmp_path: Path, model_parts, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from models.market import Timeframe
    from models.shadow import ShadowAction
    from services.forward_shadow import ForwardInput, ForwardShadowWorker

    database = _database(tmp_path)
    session_id = _seed_evidence(database)
    with database.session() as session:
        forward_session = session.get(ForwardValidationSessionRecord, session_id)
        assert forward_session is not None
        session_started = forward_session.started_at

    account, symbol, tick, candles, _position = model_parts
    m5 = candles[Timeframe.M5][-1].model_copy(update={"timestamp": NOW})
    snapshot = SimpleNamespace(
        symbol=symbol,
        tick=tick,
        account=account,
        candles={**candles, Timeframe.M5: (*candles[Timeframe.M5][:-1], m5)},
    )
    decision = SimpleNamespace(
        decision=ShadowAction.BUY,
        m5_candle_timestamp=NOW,
        feature_context={"zone_id": "pair-zone-event-v1"},
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit=2020.0,
        market_regime="TREND_UP",
    )
    worker = ForwardShadowWorker(
        Settings(forward_shadow_enabled=True),
        database,
        logger=logging.getLogger("model-inference-forward-test"),
    )
    worker.session = SimpleNamespace(
        id=session_id,
        started_at=session_started - timedelta(minutes=5),
        strategy_config_hash="a" * 64,
    )
    worker.strategy = SimpleNamespace(evaluate=lambda *_args, **_kwargs: decision)
    persisted_signals: list[object] = []
    demo_calls: list[object] = []
    operation_order: list[str] = []
    worker._ensure_session_symbol = lambda _symbol: None
    worker._persist_pair_zone_evaluation = lambda *_args, **_kwargs: None
    worker._persist_signal = lambda current_decision, _snapshot: (
        persisted_signals.append(current_decision)
        or SimpleNamespace(id="signal-id", timestamp=NOW)
    )
    worker._evaluate_open_trades = lambda: _async_none()
    worker._record_health = lambda *_args, **_kwargs: None

    class _IntelligenceEngine:
        def __init__(self, _settings):
            pass

        def evaluate(self, *_args, **_kwargs):
            return SimpleNamespace()

    intelligence_record = SimpleNamespace(
        id="intelligence-row-id", candidate_id="candidate-v1", forward_session_id=session_id,
        forward_signal_id=None, pair_zone_event_id="pair-zone-event-v1",
    )
    import services.intelligence as intelligence_module

    monkeypatch.setattr(intelligence_module, "StrategyIntelligenceEngine", _IntelligenceEngine)
    monkeypatch.setattr(
        intelligence_module,
        "persist_intelligence_record",
        lambda *_args, **_kwargs: intelligence_record,
        raising=False,
    )
    monkeypatch.setattr(
        intelligence_module,
        "link_intelligence_forward_provenance",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "services.model_inference.persist_advisory_evaluation",
        lambda *_args, **_kwargs: (
            operation_order.append("inference")
            or (_ for _ in ()).throw(RuntimeError("sensitive-token traceback-secret"))
        ),
    )

    async def _demo_handler(**kwargs):
        operation_order.append("demo_handler")
        demo_calls.append(kwargs)

    worker.execution_handler = _demo_handler
    risk_evidence = object()
    item = ForwardInput(snapshot=snapshot, market_snapshot_id=None, risk=risk_evidence)
    with caplog.at_level(logging.WARNING, logger="model-inference-forward-test"):
        await worker._process(item)

    assert persisted_signals == [decision]
    assert len(demo_calls) == 1
    assert demo_calls[0]["decision"] is decision
    assert demo_calls[0]["risk"] is risk_evidence
    assert operation_order == ["demo_handler", "inference"]
    assert "sensitive-token" not in caplog.text
    assert "traceback-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    with database.session() as session:
        assert session.scalar(select(ModelInferenceEvaluationRecord)) is None
    database.dispose()


async def _async_none() -> None:
    return None


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def test_0017_to_0018_downgrade_reupgrade_preserves_existing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "model-inference-migration.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "20260925_0017")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO configuration_versions "
                "(id, version, timestamp, configuration, checksum, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("existing-config", "keep", "2026-09-25 00:00:00", "{}", "c" * 64, 1),
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(database.engine)
        assert "model_inference_evaluations" in inspector.get_table_names()
        assert {"evaluation_key", "input_fingerprint", "input_json"} <= {
            column["name"] for column in inspector.get_columns("model_inference_evaluations")
        }
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT configuration FROM configuration_versions WHERE id = ?",
                ("existing-config",),
            ).scalar_one() == "{}"
    finally:
        database.dispose()

    command.downgrade(config, "20260925_0017")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "model_inference_evaluations" not in inspect(database.engine).get_table_names()
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version FROM configuration_versions WHERE id = ?", ("existing-config",)
            ).scalar_one() == "keep"
    finally:
        database.dispose()

    command.upgrade(config, "head")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "model_inference_evaluations" in inspect(database.engine).get_table_names()
    finally:
        database.dispose()
