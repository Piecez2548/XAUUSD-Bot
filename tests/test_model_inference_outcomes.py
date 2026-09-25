from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select

from persistence.database import Database
from persistence.orm import (
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    ModelInferenceEvaluationRecord,
    StrategyIntelligenceRecord,
)
from services.model_inference_outcomes import (
    OutcomeCursor,
    list_inference_outcome_evaluations,
    summarize_inference_outcomes,
)

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
TERMINAL_STATES = {"TP", "SL", "AMBIGUOUS", "EXPIRED"}


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'inference-outcomes.db').as_posix()}")
    database.create_schema()
    return database


def _seed_evaluation(
    database: Database,
    candidate_id: str,
    *,
    classification: str = "SUPPORTIVE",
    state: str | None = "TP",
    session_id: str = "forward-outcomes-v1",
    no_signal: bool = False,
    malformed_source_link: bool = False,
    evaluated_at: datetime = NOW,
) -> str:
    with database.session() as session:
        forward_session = session.get(ForwardValidationSessionRecord, session_id)
        if forward_session is None:
            forward_session = ForwardValidationSessionRecord(
                id=session_id,
                session_id=session_id,
                strategy_id="pair_zone_v1",
                strategy_version="pair-zone-1",
                strategy_config_hash="a" * 64,
                started_at=NOW - timedelta(hours=1),
                source_identity="disposable-outcome-test",
                symbol="XAUUSD",
                timeframes_json=["M5", "M15"],
                rr=2.0,
                cost_policy_json={},
                status="ACTIVE",
                execution_allowed=False,
            )
            session.add(forward_session)
            session.flush()

        signal = trade = None
        if not no_signal:
            signal = ForwardSignalRecord(
                signal_id=f"forward-signal-{candidate_id}",
                session_id=forward_session.id,
                timestamp=NOW
                - timedelta(minutes=5)
                + timedelta(
                    microseconds=int(hashlib.sha256(candidate_id.encode()).hexdigest()[:8], 16)
                    % 240_000_000
                ),
                decision="BUY",
                zone_id=f"zone-{candidate_id}",
                entry_price=2000.0,
                stop_loss=1990.0,
                risk_distance=10.0,
                rr=2.0,
                take_profit=2020.0,
                h1_context_json={},
                confirmation_candle_json={},
                market_observation_json={},
                strategy_hash="a" * 64,
                execution_allowed=False,
            )
            session.add(signal)
            session.flush()
            if state is not None:
                is_ambiguous = state == "AMBIGUOUS"
                trade = ForwardTradeRecord(
                    trade_id=f"forward-trade-{candidate_id}",
                    session_id=forward_session.id,
                    signal_id=signal.id,
                    timestamp=signal.timestamp,
                    side="BUY",
                    state=state,
                    entry_price=2000.0,
                    stop_loss=1990.0,
                    take_profit=2020.0,
                    risk_distance=10.0,
                    terminal_timestamp=(NOW if state in TERMINAL_STATES else None),
                    gross_r=(
                        None
                        if is_ambiguous
                        else 1.0
                        if state == "TP"
                        else -1.0
                        if state == "SL"
                        else 0.25
                    )
                    if state in TERMINAL_STATES
                    else None,
                    net_r=(
                        None
                        if is_ambiguous
                        else 0.98
                        if state == "TP"
                        else -1.02
                        if state == "SL"
                        else 0.23
                    )
                    if state in TERMINAL_STATES
                    else None,
                    spread_points=10.0,
                    spread_observation="ACTUAL",
                    entry_slippage_points=0.5,
                    exit_slippage_points=0.5,
                    commission_r=0.02,
                    total_cost_r=0.02,
                    evaluated_at=(NOW if state in TERMINAL_STATES else None),
                    reason_code=("TEST_TERMINAL" if state in TERMINAL_STATES else None),
                    execution_allowed=False,
                )
                session.add(trade)
                session.flush()

        source = StrategyIntelligenceRecord(
            candidate_id=candidate_id,
            symbol="XAUUSD",
            strategy="pair_zone_v1",
            strategy_version="pair-zone-1",
            intelligence_version="intelligence-v1",
            intelligence_runtime_version="intelligence-v1",
            evidence_version="evidence-v1",
            detected_at=NOW - timedelta(minutes=10),
            timeframe="M15/M5",
            direction="BUY",
            state="CONFIRMED",
            score=80.0,
            confidence_band="HIGH",
            alert_decision={
                "SUPPORTIVE": "ALERT",
                "CAUTION": "BLOCK",
                "OBSERVATION_ONLY": "OBSERVE",
            }.get(classification, "ALERT"),
            blockers_json=["test-blocker"] if classification == "CAUTION" else [],
            warnings_json=[],
            context_json={},
            evidence_json=[],
            score_components_json=[],
            source="test",
            pair_zone_event_id=f"zone-{candidate_id}",
            forward_session_id=forward_session.id,
            forward_signal_id=signal.id if signal else None,
            forward_trade_id=(None if malformed_source_link or trade is None else trade.id),
            execution_allowed=False,
        )
        session.add(source)
        session.flush()

        evaluation = ModelInferenceEvaluationRecord(
            evaluation_key=hashlib.sha256(f"eval:{candidate_id}".encode()).hexdigest(),
            inference_version="deterministic_advisory_v1",
            evaluated_at=evaluated_at,
            source_intelligence_record_id=source.id,
            source_candidate_id=candidate_id,
            forward_session_id=forward_session.id,
            forward_signal_id=signal.id if signal else None,
            pair_zone_event_id=source.pair_zone_event_id,
            strategy_config_hash=forward_session.strategy_config_hash,
            input_fingerprint=hashlib.sha256(f"input:{candidate_id}".encode()).hexdigest(),
            input_json={},
            advisory_classification=classification,
            reason_codes_json=["TEST_ONLY"],
            execution_allowed=False,
        )
        session.add(evaluation)
        session.flush()
        return evaluation.id


@pytest.mark.parametrize(
    ("classification", "state", "expected_net_r"),
    [
        ("SUPPORTIVE", "TP", 0.98),
        ("CAUTION", "SL", -1.02),
        ("OBSERVATION_ONLY", "EXPIRED", 0.23),
    ],
)
def test_each_advisory_classification_links_to_completed_forward_outcome(
    tmp_path: Path, classification: str, state: str, expected_net_r: float
) -> None:
    database = _database(tmp_path)
    try:
        _seed_evaluation(database, classification, classification=classification, state=state)
        page = list_inference_outcome_evaluations(database)
        assert len(page.items) == 1
        row = page.items[0]
        assert row.classification == classification
        assert row.disposition == "COMPLETED"
        assert row.outcome_state == state
        assert row.net_r == expected_net_r
        assert row.forward_trade_id is not None
        assert row.forward_signal_id is not None
    finally:
        database.dispose()


def test_ambiguous_forward_outcome_is_completed_without_fabricated_r() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        database = _database(Path(directory))
        try:
            _seed_evaluation(database, "ambiguous", state="AMBIGUOUS")
            row = list_inference_outcome_evaluations(database).items[0]
            assert row.disposition == "COMPLETED"
            assert row.outcome_state == "AMBIGUOUS"
            assert row.net_r is None
            assert row.gross_r is None
        finally:
            database.dispose()


def test_open_forward_trade_remains_pending_and_late_completion_is_visible() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        database = _database(Path(directory))
        try:
            _seed_evaluation(database, "late", state="OPEN")
            first = list_inference_outcome_evaluations(database).items[0]
            assert first.disposition == "PENDING"
            with database.session() as session:
                trade = session.scalar(select(ForwardTradeRecord))
                assert trade is not None
                trade.state = "TP"
                trade.terminal_timestamp = NOW
                trade.evaluated_at = NOW
                trade.gross_r = 1.0
                trade.net_r = 0.98
            later = list_inference_outcome_evaluations(database).items[0]
            assert later.disposition == "COMPLETED"
            assert later.net_r == 0.98
        finally:
            database.dispose()


@pytest.mark.parametrize(
    "malformation",
    ["missing_trade_link", "candidate_mismatch", "wrong_session", "signal_config_mismatch"],
)
def test_malformed_provenance_is_unresolved(tmp_path: Path, malformation: str) -> None:
    database = _database(tmp_path)
    try:
        evaluation_id = _seed_evaluation(
            database,
            f"bad-{malformation}",
            malformed_source_link=malformation == "missing_trade_link",
        )
        with database.session() as session:
            evaluation = session.get(ModelInferenceEvaluationRecord, evaluation_id)
            source = session.get(
                StrategyIntelligenceRecord, evaluation.source_intelligence_record_id
            )
            assert evaluation is not None and source is not None
            if malformation == "candidate_mismatch":
                source.candidate_id = "different-candidate"
            elif malformation == "wrong_session":
                other = ForwardValidationSessionRecord(
                    id="other-session",
                    session_id="other-session",
                    strategy_id="pair_zone_v1",
                    strategy_version="pair-zone-1",
                    strategy_config_hash="b" * 64,
                    started_at=NOW - timedelta(hours=1),
                    source_identity="disposable-outcome-test",
                    symbol="XAUUSD",
                    timeframes_json=["M5"],
                    rr=2.0,
                    cost_policy_json={},
                    status="ACTIVE",
                    execution_allowed=False,
                )
                session.add(other)
                session.flush()
                evaluation.forward_session_id = other.id
                evaluation.strategy_config_hash = other.strategy_config_hash
            elif malformation == "signal_config_mismatch":
                signal = session.scalar(select(ForwardSignalRecord))
                assert signal is not None
                signal.strategy_hash = "b" * 64
        row = next(
            item
            for item in list_inference_outcome_evaluations(database).items
            if item.evaluation_id == evaluation_id
        )
        assert row.disposition == "UNRESOLVED"
        assert row.net_r is None
    finally:
        database.dispose()


def test_candidate_without_forward_signal_is_not_eligible_not_pending(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        _seed_evaluation(database, "no-signal", no_signal=True, state=None)
        row = list_inference_outcome_evaluations(database).items[0]
        assert row.disposition == "NOT_ELIGIBLE"
        assert row.outcome_state is None
    finally:
        database.dispose()


def test_repeated_read_is_idempotent_and_summary_uses_existing_net_r_semantics(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    try:
        _seed_evaluation(database, "winner", classification="SUPPORTIVE", state="TP")
        _seed_evaluation(database, "loser", classification="CAUTION", state="SL")
        first = summarize_inference_outcomes(database, page_size=1)
        second = summarize_inference_outcomes(database, page_size=1)
        assert first == second
        assert first["evaluation_count"] == 2
        assert first["completed_outcome_count"] == 2
        supportive = first["by_classification"]["SUPPORTIVE"]
        caution = first["by_classification"]["CAUTION"]
        assert supportive["outcome_counts"] == {"TP": 1}
        assert supportive["win_count"] == 1
        assert supportive["average_net_r"] == pytest.approx(0.98)
        assert supportive["net_r_sample_count"] == 1
        assert caution["outcome_counts"] == {"SL": 1}
        assert caution["loss_count"] == 1
        assert caution["average_net_r"] == pytest.approx(-1.02)
        with database.session() as session:
            assert len(list(session.scalars(select(ModelInferenceEvaluationRecord)))) == 2
            assert len(list(session.scalars(select(ForwardTradeRecord)))) == 2
    finally:
        database.dispose()


def test_keyset_pages_are_bounded_and_do_not_skip_timestamp_ties(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    statements: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "model_inference_evaluations" in statement.lower():
            statements.append((statement, parameters))

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        for index in range(23):
            _seed_evaluation(
                database,
                f"page-{index:02d}",
                no_signal=True,
                state=None,
                evaluated_at=NOW,
            )
        seen: list[str] = []
        cursor: OutcomeCursor | None = None
        pages = 0
        while True:
            page = list_inference_outcome_evaluations(database, page_size=5, after=cursor)
            assert len(page.items) <= 5
            seen.extend(item.candidate_id for item in page.items)
            pages += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(seen) == 23
        assert len(set(seen)) == 23
        assert pages == 5
        assert len(statements) >= pages
        for statement, parameters in statements[-pages:]:
            assert "limit" in statement.lower()
            assert tuple(parameters)[-2:] == (6, 0)
        summary = summarize_inference_outcomes(database, page_size=5)
        assert summary["evaluation_count"] == 23
        assert summary["not_eligible_count"] == 23
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
        database.dispose()


def test_query_failure_is_isolated_and_performs_no_writes(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        _seed_evaluation(database, "failure", state="TP")

        class FailingReadDatabase:
            def session(self):
                raise RuntimeError("read-only evaluation unavailable")

        with pytest.raises(RuntimeError):
            list_inference_outcome_evaluations(FailingReadDatabase())
        with database.session() as session:
            assert (
                session.scalar(
                    select(ModelInferenceEvaluationRecord).where(
                        ModelInferenceEvaluationRecord.source_candidate_id == "failure"
                    )
                )
                is not None
            )
            assert (
                session.scalar(
                    select(ForwardTradeRecord).where(
                        ForwardTradeRecord.trade_id == "forward-trade-failure"
                    )
                ).state
                == "TP"
            )
    finally:
        database.dispose()


def test_individual_row_derivation_failure_is_unresolved_without_aborting_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.model_inference_outcomes as outcomes

    database = _database(tmp_path)
    original_derive = outcomes._derive_row
    try:
        _seed_evaluation(database, "malformed-row", state="TP")
        _seed_evaluation(database, "valid-row", state="SL")

        def fail_one_row(evaluation, *related):
            if evaluation.source_candidate_id == "malformed-row":
                raise ValueError("sensitive malformed evidence")
            return original_derive(evaluation, *related)

        monkeypatch.setattr(outcomes, "_derive_row", fail_one_row)
        page = outcomes.list_inference_outcome_evaluations(database)
        assert len(page.items) == 2
        failed = next(row for row in page.items if row.candidate_id == "malformed-row")
        valid = next(row for row in page.items if row.candidate_id == "valid-row")
        assert failed.disposition == "UNRESOLVED"
        assert failed.outcome_state is None
        assert failed.net_r is None
        assert valid.disposition == "COMPLETED"
        assert valid.outcome_state == "SL"

        summary = outcomes.summarize_inference_outcomes(database)
        assert summary["evaluation_count"] == 2
        assert summary["unresolved_count"] == 1
        assert summary["completed_outcome_count"] == 1
    finally:
        database.dispose()


def test_observer_has_no_trading_authority_dependencies() -> None:
    source = Path("services/model_inference_outcomes.py").read_text(encoding="utf-8").lower()
    assert "order_send" not in source
    assert "demoexecutionservice" not in source
    assert "mt5" not in source
    assert "update(" not in source
    assert "session.add" not in source
