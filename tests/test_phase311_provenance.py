from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings
from persistence.database import Database
from persistence.orm import (
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    StrategyIntelligenceRecord,
)
from research.intelligence_evaluation import (
    evaluate_records,
    observations_from_persisted_rows,
)
from services.forward_shadow import ForwardInput, ForwardShadowWorker
from services.intelligence import (
    ProvenanceConflictError,
    link_intelligence_forward_provenance,
)

OBSERVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def database(tmp_path) -> Database:
    result = Database(f"sqlite:///{(tmp_path / 'phase311.db').as_posix()}")
    result.create_schema()
    return result


def seed_forward_graph(db: Database, *, session_suffix: str = "a") -> tuple[str, str, str]:
    with db.session() as session:
        forward_session = ForwardValidationSessionRecord(
            session_id=f"forward-{session_suffix}",
            strategy_id="pair_zone_v1",
            strategy_version="1.0.0",
            strategy_config_hash="a" * 64,
            started_at=OBSERVED_AT - timedelta(hours=1),
            source_identity="fixture",
            symbol="XAUUSDm",
            timeframes_json=["M5", "M15", "H1"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        )
        session.add(forward_session)
        session.flush()
        signal = ForwardSignalRecord(
            signal_id=f"forward-signal-{session_suffix}",
            session_id=forward_session.id,
            timestamp=OBSERVED_AT,
            decision="BUY",
            zone_id="pz-canonical-1",
            entry_price=2000.0,
            stop_loss=1990.0,
            risk_distance=10.0,
            rr=2.0,
            take_profit=2020.0,
            pair_first_timestamp=OBSERVED_AT - timedelta(minutes=30),
            pair_second_timestamp=OBSERVED_AT - timedelta(minutes=15),
            h1_context_json={},
            confirmation_candle_json={},
            market_observation_json={},
            strategy_hash="b" * 64,
            execution_allowed=False,
        )
        session.add(signal)
        session.flush()
        trade = ForwardTradeRecord(
            trade_id=f"forward-trade-{session_suffix}",
            session_id=forward_session.id,
            signal_id=signal.id,
            timestamp=OBSERVED_AT,
            side="BUY",
            state="OPEN",
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            risk_distance=10.0,
            spread_points=20.0,
            spread_observation="FIXTURE",
            entry_slippage_points=0.0,
            exit_slippage_points=0.0,
            commission_r=0.0,
            execution_allowed=False,
        )
        session.add(trade)
        session.flush()
        return forward_session.id, signal.id, trade.id


def seed_candidate(db: Database, *, candidate_id: str = "candidate-1") -> None:
    with db.session() as session:
        session.add(
            StrategyIntelligenceRecord(
                candidate_id=candidate_id,
                symbol="XAUUSDm",
                strategy="pair_zone_v1",
                strategy_version="1.0.0",
                intelligence_version="phase3.0_intelligence_v1",
                intelligence_runtime_version="phase3.0_intelligence_v1",
                evidence_version="evidence_v1",
                detected_at=OBSERVED_AT,
                timeframe="M15/M5",
                direction="BUY",
                state="CONFIRMED",
                score=70.0,
                confidence_band="HIGH",
                alert_decision="ALERT",
                blockers_json=[],
                warnings_json=[],
                context_json={"data_status": "READY"},
                evidence_json=[],
                score_components_json=[],
                source="ExactPairAdapter",
                pair_zone_event_id="pz-canonical-1",
                execution_allowed=False,
            )
        )


def test_provenance_round_trip_and_forward_outcome_linkage(tmp_path) -> None:
    db = database(tmp_path)
    session_id, signal_id, trade_id = seed_forward_graph(db)
    seed_candidate(db)

    linked = link_intelligence_forward_provenance(
        db,
        candidate_id="candidate-1",
        forward_session_id=session_id,
        forward_signal_id=signal_id,
    )
    repeat = link_intelligence_forward_provenance(
        db,
        candidate_id="candidate-1",
        forward_session_id=session_id,
        forward_signal_id=signal_id,
    )
    assert linked.id == repeat.id
    assert linked.forward_trade_id == trade_id
    with db.session() as session:
        stored = session.get(StrategyIntelligenceRecord, linked.id)
        assert stored is not None
        assert stored.pair_zone_event_id == "pz-canonical-1"
        assert stored.intelligence_version == "phase3.0_intelligence_v1"
        assert stored.execution_allowed is False
    db.dispose()


def test_conflicting_candidate_signal_and_cross_session_linkage_fail_closed(tmp_path) -> None:
    db = database(tmp_path)
    first_session, first_signal, _ = seed_forward_graph(db, session_suffix="a")
    second_session, second_signal, _ = seed_forward_graph(db, session_suffix="b")
    seed_candidate(db)

    link_intelligence_forward_provenance(
        db,
        candidate_id="candidate-1",
        forward_session_id=first_session,
        forward_signal_id=first_signal,
    )
    with pytest.raises(ProvenanceConflictError, match="different session|different signal"):
        link_intelligence_forward_provenance(
            db,
            candidate_id="candidate-1",
            forward_session_id=second_session,
            forward_signal_id=second_signal,
        )
    with pytest.raises(ProvenanceConflictError, match="different session"):
        link_intelligence_forward_provenance(
            db,
            candidate_id="candidate-1",
            forward_session_id=first_session,
            forward_signal_id=second_signal,
        )
    db.dispose()


def test_future_or_conflicting_outcome_linkage_is_rejected(tmp_path) -> None:
    db = database(tmp_path)
    session_id, signal_id, trade_id = seed_forward_graph(db)
    seed_candidate(db)
    with db.session() as session:
        trade = session.get(ForwardTradeRecord, trade_id)
        assert trade is not None
        trade.state = "TP"
        trade.terminal_timestamp = OBSERVED_AT - timedelta(minutes=1)
    with pytest.raises(ProvenanceConflictError, match="not after"):
        link_intelligence_forward_provenance(
            db,
            candidate_id="candidate-1",
            forward_session_id=session_id,
            forward_signal_id=signal_id,
        )
    db.dispose()


def test_evaluator_reads_resolved_outcome_by_explicit_trade_id_only() -> None:
    intelligence = SimpleNamespace(
        candidate_id="candidate-1",
        symbol="XAUUSDm",
        strategy="pair_zone_v1",
        strategy_version="1.0.0",
        evidence_version="evidence_v1",
        detected_at=OBSERVED_AT,
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
        forward_session_id="session-id",
        forward_signal_id="signal-id",
        forward_trade_id="trade-id",
        execution_allowed=False,
    )
    signal = SimpleNamespace(id="signal-id", decision="BUY")
    trade = SimpleNamespace(
        id="trade-id",
        signal_id="unrelated-signal",
        state="TP",
        net_r=1.0,
        terminal_timestamp=OBSERVED_AT + timedelta(minutes=5),
    )
    observations = observations_from_persisted_rows(
        [intelligence], forward_signal_rows=[signal], forward_trade_rows=[trade]
    )
    assert observations[0].outcome_id == "trade-id"
    assert observations[0].outcome_status == "TP"
    result = evaluate_records(observations, generated_at=OBSERVED_AT)
    assert result["population_metrics"]["FORWARD_SHADOW"]["metrics"]["resolved_count"] == 1
    assert result["population_metrics"]["HISTORICAL_REPLAY"]["metrics"]["observation_count"] == 0


def test_historical_and_forward_populations_remain_distinct() -> None:
    base = {
        "symbol": "XAUUSDm",
        "timeframe": "M15/M5",
        "timestamp": OBSERVED_AT.isoformat(),
        "score": 70,
        "disposition": "ALERT",
        "outcome_status": "TP_HIT",
        "outcome_realized_r": 1.0,
        "outcome_timestamp": (OBSERVED_AT + timedelta(minutes=5)).isoformat(),
        "execution_allowed": False,
    }
    historical = {
        **base,
        "observation_id": "research-decision-1",
        "population": "HISTORICAL_REPLAY",
    }
    forward = {**base, "observation_id": "candidate-1", "population": "FORWARD_SHADOW"}
    result = evaluate_records([historical, forward], generated_at=OBSERVED_AT)
    assert result["data_coverage"]["populations"] == {
        "FORWARD_SHADOW": 1,
        "HISTORICAL_REPLAY": 1,
    }
    assert result["population_metrics"]["HISTORICAL_REPLAY"]["metrics"]["resolved_count"] == 1
    assert result["population_metrics"]["FORWARD_SHADOW"]["metrics"]["resolved_count"] == 1


def test_forward_shadow_propagates_pair_zone_id_without_changing_decision(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database(tmp_path)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'forward.db').as_posix()}")
    worker = ForwardShadowWorker(settings, db, logger=logging.getLogger("phase311-provenance"))
    timestamp = OBSERVED_AT + timedelta(hours=1)
    worker.session = SimpleNamespace(id="session-id", started_at=timestamp - timedelta(minutes=5))
    decision = SimpleNamespace(
        decision=SimpleNamespace(value="BUY"),
        m5_candle_timestamp=timestamp,
        feature_context={"zone_id": "pz-canonical-1", "zone_created_at": timestamp.isoformat()},
    )
    calls: dict[str, object] = {}

    class FakeIntelligence:
        version = "phase3.0_intelligence_v1"

        def __init__(self, _settings) -> None:
            pass

        def evaluate(self, *_args, **_kwargs):
            return SimpleNamespace(candidate=SimpleNamespace(execution_allowed=False))

    def persist(_database, _result, **kwargs):
        calls["persist"] = kwargs
        return SimpleNamespace(candidate_id="candidate-1")

    def link(_database, **kwargs):
        calls["link"] = kwargs
        return None

    async def no_open_trade_evaluation() -> None:
        return None

    monkeypatch.setattr(worker, "_ensure_session_symbol", lambda _symbol: None)
    monkeypatch.setattr(worker.strategy, "evaluate", lambda *args, **kwargs: decision)
    monkeypatch.setattr(
        worker,
        "_persist_signal",
        lambda *_args, **_kwargs: SimpleNamespace(id="signal-id", timestamp=timestamp),
    )
    monkeypatch.setattr(worker, "_evaluate_open_trades", no_open_trade_evaluation)
    monkeypatch.setattr(worker, "_record_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("services.intelligence.StrategyIntelligenceEngine", FakeIntelligence)
    monkeypatch.setattr("services.intelligence.persist_intelligence_record", persist)
    monkeypatch.setattr("services.intelligence.link_intelligence_forward_provenance", link)

    snapshot = SimpleNamespace(
        symbol=SimpleNamespace(name="XAUUSDm"),
        candles={"M5": (SimpleNamespace(timestamp=timestamp),)},
    )
    asyncio.run(
        worker._process(ForwardInput(snapshot=snapshot, market_snapshot_id="market", risk=None))
    )
    assert calls["persist"] == {
        "forward_session_id": "session-id",
        "pair_zone_event_id": "pz-canonical-1",
    }
    assert calls["link"] == {
        "candidate_id": "candidate-1",
        "forward_session_id": "session-id",
        "forward_signal_id": "signal-id",
    }
    assert decision.decision.value == "BUY"
    db.dispose()
