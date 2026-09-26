from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import select

from config.settings import Settings
from models.intelligence import (
    AlertDecision,
    CandidateState,
    ConfidenceBand,
    Direction,
    EvidenceItem,
    EvidencePolarity,
    ManualOBObservation,
    TrendDirection,
)
from models.market import AccountState, Candle, MarketSnapshot, SymbolSpecification, Tick, Timeframe
from models.observatory import RiskSnapshot
from persistence.database import Database
from persistence.orm import StrategyIntelligenceRecord
from services.intelligence import (
    ALERT_SCORE_THRESHOLD,
    INTELLIGENCE_CONFIG_PATH,
    INTELLIGENCE_CONTRACT,
    AlertPolicy,
    EvidenceScorer,
    MarketContextEngine,
    ManualOBAdapter,
    StrategyIntelligenceEngine,
    load_intelligence_contract,
    persist_intelligence_record,
    serialize_for_forward_shadow,
)


def candle(timestamp: datetime, base: float, *, direction: int = 1) -> Candle:
    close = base + direction * 0.5
    return Candle(
        timestamp=timestamp, raw_timestamp=int(timestamp.timestamp()),
        open=base, high=max(base, close) + 0.8, low=min(base, close) - 0.8,
        close=close, tick_volume=100, spread=20, real_volume=0,
    )


def risk() -> RiskSnapshot:
    return RiskSnapshot(
        equity=10_000, balance=10_000, open_risk_percent=0, open_risk_amount=0,
        remaining_risk_percent=6, risk_per_position=tuple(), max_trade_risk_percent=2,
        max_aggregate_risk_percent=6, open_positions_count=0, unbounded_positions_count=0,
        free_margin=10_000,
    )


def snapshot(*, count: int = 80, start: datetime | None = None) -> MarketSnapshot:
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    # Alternating higher swing points creates a deterministic HH/HL sequence
    # without using the final bar as a pivot before its right-side confirmation.
    values = [100 + index * 0.4 + (1.0 if index % 2 else 0.0) for index in range(count)]
    candles = {}
    for timeframe, minutes in ((Timeframe.M5, 5), (Timeframe.M15, 15)):
        series = tuple(
            candle(start + timedelta(minutes=index * minutes), values[index], direction=1)
            for index in range(count)
        )
        candles[timeframe] = series
    candles[Timeframe.H1] = tuple(candle(start + timedelta(hours=index), 100 + index, direction=1) for index in range(8))
    candles[Timeframe.H4] = tuple(candle(start + timedelta(hours=index * 4), 100 + index, direction=1) for index in range(2))
    account = AccountState(
        balance=10_000, equity=10_000, margin=0, free_margin=10_000,
        margin_level=0, profit=0, leverage=100, currency="USD",
        server="research", trade_mode=0, trade_mode_name="research",
    )
    symbol = SymbolSpecification(
        name="XAUUSD", bid=values[-1], ask=values[-1] + 0.2, spread=20,
        digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=1,
        trade_tick_value_profit=1, trade_tick_value_loss=1, contract_size=100,
        volume_min=0.01, volume_max=100, volume_step=0.01,
        trade_mode=4, trade_mode_name="research",
    )
    timestamp = candles[Timeframe.M5][-1].timestamp
    return MarketSnapshot(
        account=account, symbol=symbol,
        tick=Tick(timestamp=timestamp, raw_timestamp=int(timestamp.timestamp()),
                  bid=values[-1], ask=values[-1] + 0.2, last=values[-1], volume=0, flags=0),
        positions=tuple(), candles=candles, generated_at=timestamp,
    )


def test_context_is_structure_based_and_causal():
    current = snapshot()
    context = MarketContextEngine().build(current)
    assert context.data_status.value == "READY"
    assert context.m15_trend == TrendDirection.BULLISH
    assert context.m5_trend == TrendDirection.BULLISH
    assert context.trend == TrendDirection.BULLISH
    assert all(point.confirmed_at <= context.as_of for point in context.m5_swing_points)

    future = candle(current.generated_at + timedelta(minutes=5), 500, direction=-1)
    future_candles = dict(current.candles)
    future_candles[Timeframe.M5] = (*future_candles[Timeframe.M5], future)
    future_snapshot = current.model_copy(update={"candles": future_candles})
    before = MarketContextEngine().build(current, as_of=current.generated_at)
    after = MarketContextEngine().build(future_snapshot, as_of=current.generated_at)
    assert before.model_dump(mode="json") == after.model_dump(mode="json")


def test_context_rejects_gaps_and_stale_data():
    current = snapshot()
    broken = list(current.candles[Timeframe.M5])
    broken.pop(10)
    snapshot_with_gap = current.model_copy(update={"candles": {**current.candles, Timeframe.M5: tuple(broken)}})
    gap_context = MarketContextEngine().build(snapshot_with_gap)
    assert gap_context.data_status.value == "INSUFFICIENT_DATA"
    assert "TIMEFRAME_GAP_M5" in gap_context.data_reasons

    stale = MarketContextEngine(stale_after=timedelta(minutes=1)).build(
        current, as_of=current.generated_at + timedelta(minutes=5)
    )
    assert stale.data_status.value == "STALE"


def test_versioned_yaml_contract_drives_runtime_constants():
    contract = load_intelligence_contract(INTELLIGENCE_CONFIG_PATH)
    assert contract == INTELLIGENCE_CONTRACT
    assert contract.scoring_weights["STRUCTURE"] == 0.25
    assert contract.confidence_thresholds["HIGH"] == 70
    assert contract.alert_score_threshold == ALERT_SCORE_THRESHOLD == 70
    assert contract.manual_ob_rejection_confirmation == "NOT_ACCEPTED"
    assert set(("APPROACHING_OB", "ENTERED_OB")).issubset(contract.manual_ob_allowed_states)


def test_invalid_intelligence_contract_fails_closed(tmp_path):
    invalid = tmp_path / "invalid-intelligence.yaml"
    invalid.write_text(
        """
intelligence_version: phase3.0_intelligence_v1
score_is_probability: true
""",
        encoding="utf-8",
    )
    with pytest.raises((ValueError, RuntimeError), match="missing fields|probability"):
        load_intelligence_contract(invalid)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("intelligence_version", "phase3.0_unknown", "unsupported"),
        ("alert_score_threshold", 101, "alert score threshold is invalid"),
        ("manual_ob", {"rejection_confirmation": "ACCEPTED", "allowed_states": ["APPROACHING_OB"]}, "NOT_ACCEPTED"),
    ],
)
def test_unsafe_yaml_contract_variants_fail_closed(tmp_path, field, value, message):
    config = yaml.safe_load(INTELLIGENCE_CONFIG_PATH.read_text(encoding="utf-8"))
    config[field] = value
    path = tmp_path / f"invalid-{field}.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_intelligence_contract(path)


def test_invalid_weights_and_malformed_yaml_fail_closed(tmp_path):
    config = yaml.safe_load(INTELLIGENCE_CONFIG_PATH.read_text(encoding="utf-8"))
    config["weights"]["STRUCTURE"] = 0.01
    invalid_weights = tmp_path / "invalid-weights.yaml"
    invalid_weights.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="weights must be non-negative and sum to 1"):
        load_intelligence_contract(invalid_weights)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("intelligence_version: [", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unable to load"):
        load_intelligence_contract(malformed)


def test_scoring_is_explicit_and_blockers_cannot_be_overridden():
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    evidence = (
        EvidenceItem(type="trend_structure", source="test", timeframe="M15", value="BULLISH", normalized_value=1, polarity=EvidencePolarity.SUPPORTIVE, strength=1, timestamp=timestamp, rule_id="STRUCTURE"),
        EvidenceItem(type="pair_zone_state", source="test", timeframe="M15", value="CONFIRMED", normalized_value=1, polarity=EvidencePolarity.SUPPORTIVE, strength=1, timestamp=timestamp, rule_id="ZONE"),
    )
    score, band, components = EvidenceScorer().score(evidence)
    assert score > 50
    assert band in {ConfidenceBand.MODERATE, ConfidenceBand.HIGH}
    assert {item.category for item in components} == {"STRUCTURE", "ZONE_CONTEXT", "ENTRY_CONTEXT", "MOMENTUM", "VOLATILITY", "SESSION", "CONTRADICTIONS"}

    blocked_score, _, _ = EvidenceScorer().score(evidence, blockers=("INVALIDATED_ZONE",))
    assert blocked_score == score
    candidate = StrategyIntelligenceEngine(Settings()).evaluate(snapshot(), risk=risk()).candidate
    assert candidate.execution_allowed is False


def test_alert_policy_deduplicates_same_state_within_cooldown():
    candidate = StrategyIntelligenceEngine(Settings()).evaluate(snapshot(), risk=risk()).candidate
    candidate = candidate.model_copy(update={"state": CandidateState.CANDIDATE, "score": ALERT_SCORE_THRESHOLD})
    assert AlertPolicy().decide(candidate) == AlertDecision.ALERT
    repeated = candidate.model_copy(update={"detected_at": candidate.detected_at + timedelta(minutes=1)})
    assert AlertPolicy().decide(repeated, candidate) == AlertDecision.OBSERVE


def test_manual_ob_never_confirms_rejection():
    context = MarketContextEngine().build(snapshot())
    for state, expected in (("APPROACHING_OB", CandidateState.APPROACHING),
                            ("ENTERED_OB", CandidateState.IN_ZONE),
                            ("POTENTIAL_REJECTION", CandidateState.CANDIDATE)):
        observation = ManualOBObservation(
            observation_id=f"manual-{state}", symbol="XAUUSD", direction=Direction.BUY,
            state=state, observed_at=context.as_of,
        )
        candidate = ManualOBAdapter().adapt(observation, context)
        assert candidate.state == expected
        assert candidate.alert_decision == AlertDecision.OBSERVE
        assert "MANUAL_REJECTION_NOT_ACCEPTED" in candidate.warnings
        assert candidate.execution_allowed is False


def test_intelligence_serialization_and_forward_persistence_are_versioned(tmp_path):
    result = StrategyIntelligenceEngine(Settings()).evaluate(snapshot(), risk=risk())
    payload = serialize_for_forward_shadow(result)
    assert payload["execution_allowed"] is False
    assert payload["intelligence_version"].startswith("phase3.0_")
    database = Database(f"sqlite:///{tmp_path / 'intelligence.db'}", project_root=tmp_path)
    database.create_schema()
    row = persist_intelligence_record(database, result)
    assert row.candidate_id == result.candidate.candidate_id
    duplicate = persist_intelligence_record(database, result, forward_session_id="forward-session-1")
    assert duplicate.id == row.id
    with database.session() as session:
        stored = session.get(StrategyIntelligenceRecord, row.id)
        assert stored is not None
        assert stored.execution_allowed is False
        assert stored.evidence_version == result.candidate.evidence_version
        assert stored.forward_session_id is None
        assert len(session.scalars(select(StrategyIntelligenceRecord)).all()) == 1
    database.dispose()


def _structured_m15(count: int, start: datetime, *, step: float = 0.3) -> tuple[Candle, ...]:
    values = [100 + index * step + (1.5 if index % 2 else 0.0) for index in range(count)]
    return tuple(
        candle(start + timedelta(minutes=15 * index), value)
        for index, value in enumerate(values)
    )


def _snapshot_with_m15(m15: tuple[Candle, ...]) -> MarketSnapshot:
    base = snapshot()
    return base.model_copy(update={
        "candles": {**base.candles, Timeframe.M15: m15},
        "generated_at": m15[-1].timestamp,
    })


def _recent_start() -> datetime:
    return snapshot().candles[Timeframe.M5][-1].timestamp - timedelta(minutes=15 * 13)


def test_m15_exact_fourteen_closed_candles_are_numerically_sufficient():
    engine = MarketContextEngine()
    sufficient = _snapshot_with_m15(_structured_m15(14, _recent_start()))
    context = engine.build(sufficient)
    assert context.data_status.value == "READY"
    assert context.m15_atr is not None

    insufficient = _snapshot_with_m15(_structured_m15(13, _recent_start()))
    context = engine.build(insufficient)
    assert context.data_status.value == "INSUFFICIENT_DATA"
    assert "INSUFFICIENT_CONTINUOUS_HISTORY_M15" in context.data_reasons


def test_m15_requires_two_confirmed_highs_and_lows_after_numeric_check():
    values = tuple(
        candle(_recent_start() + timedelta(minutes=15 * index), 100 + index)
        for index in range(14)
    )
    context = MarketContextEngine().build(_snapshot_with_m15(values))
    assert context.data_status.value == "INSUFFICIENT_DATA"
    assert context.data_reasons == ("INSUFFICIENT_STRUCTURE_M15",)

    sufficient = MarketContextEngine().build(
        _snapshot_with_m15(_structured_m15(14, _recent_start()))
    )
    highs = [point for point in sufficient.m15_swing_points if point.kind == "SWING_HIGH"]
    lows = [point for point in sufficient.m15_swing_points if point.kind == "SWING_LOW"]
    assert len(highs) >= 2
    assert len(lows) >= 2
    assert all(point.left_bars == 1 and point.right_bars == 1 for point in (*highs, *lows))
    assert all(point.confirmed_at <= sufficient.as_of for point in (*highs, *lows))


def test_m15_uses_only_the_newest_continuous_segment_and_does_not_leak_old_data():
    start = _recent_start()
    recent = _structured_m15(14, start)
    old = tuple(
        candle(start - timedelta(days=2) + timedelta(minutes=15 * index), 10_000 - index * 100)
        for index in range(8)
    )
    segmented = _snapshot_with_m15((*old, *recent))
    recent_only = _snapshot_with_m15(recent)
    engine = MarketContextEngine()
    segmented_context = engine.build(segmented)
    recent_context = engine.build(recent_only)

    assert segmented_context.data_status.value == "READY"
    assert segmented_context.latest_m15_timestamp == recent[-1].timestamp
    assert segmented_context.m15_atr == recent_context.m15_atr
    assert segmented_context.m15_structure == recent_context.m15_structure
    assert segmented_context.m15_trend == recent_context.m15_trend
    assert segmented_context.m15_swing_points == recent_context.m15_swing_points


def test_m15_old_gap_is_ignored_only_when_the_new_segment_is_independently_ready():
    start = _recent_start()
    old = _structured_m15(20, start - timedelta(days=3))
    recent = _structured_m15(14, start)
    context = MarketContextEngine().build(_snapshot_with_m15((*old, *recent)))
    assert context.data_status.value == "READY"
    assert context.latest_m15_timestamp == recent[-1].timestamp

    short_recent = _structured_m15(13, start)
    context = MarketContextEngine().build(_snapshot_with_m15((*old, *short_recent)))
    assert context.data_status.value == "INSUFFICIENT_DATA"
    assert "INSUFFICIENT_CONTINUOUS_HISTORY_M15" in context.data_reasons


def test_m15_gap_restarts_segment_instead_of_combining_history():
    start = _recent_start()
    prior = _structured_m15(14, start - timedelta(days=1))
    after_gap = _structured_m15(13, start)
    context = MarketContextEngine().build(_snapshot_with_m15((*prior, *after_gap)))
    assert context.data_status.value == "INSUFFICIENT_DATA"
    assert "INSUFFICIENT_CONTINUOUS_HISTORY_M15" in context.data_reasons


@pytest.mark.parametrize(
    "discontinuity",
    [
        timedelta(days=1), timedelta(minutes=30), timedelta(minutes=45),
        timedelta(days=2), timedelta(hours=23),
    ],
    ids=("daily", "one_bar", "two_bar", "weekend", "dst_shift"),
)
def test_repeated_data_loss_and_calendar_discontinuities_have_no_special_authority(discontinuity):
    start = _recent_start() - timedelta(days=10)
    segments = []
    cursor = start
    for _ in range(3):
        segments.extend(_structured_m15(3, cursor))
        cursor = segments[-1].timestamp + discontinuity
    segments.extend(_structured_m15(13, cursor))
    context = MarketContextEngine().build(_snapshot_with_m15(tuple(segments)))
    assert context.data_status.value == "INSUFFICIENT_DATA"
    assert "INSUFFICIENT_CONTINUOUS_HISTORY_M15" in context.data_reasons


def test_multiple_gaps_select_the_newest_segment_only():
    start = _recent_start() - timedelta(days=3)
    first = _structured_m15(20, start)
    second = _structured_m15(20, start + timedelta(days=1))
    newest = _structured_m15(14, _recent_start())
    context = MarketContextEngine().build(_snapshot_with_m15((*first, *second, *newest)))
    assert context.data_status.value == "READY"
    assert context.latest_m15_timestamp == newest[-1].timestamp
    assert all(point.pivot_timestamp >= newest[0].timestamp for point in context.m15_swing_points)


@pytest.mark.parametrize("mutation", ["duplicate", "unordered"])
def test_m15_duplicate_or_unordered_timestamps_fail_closed(mutation):
    values = list(_structured_m15(14, _recent_start()))
    if mutation == "duplicate":
        values[5] = values[4]
    else:
        values[5], values[6] = values[6], values[5]
    context = MarketContextEngine().build(_snapshot_with_m15(tuple(values)))
    assert context.data_status.value == "INSUFFICIENT_DATA"
    assert "UNSORTED_OR_DUPLICATE_M15" in context.data_reasons


def test_forming_m15_candle_is_not_used_as_closed_history():
    start = _recent_start()
    closed = _structured_m15(14, start)
    forming = candle(closed[-1].timestamp + timedelta(minutes=15), 500, direction=-1)
    context = MarketContextEngine().build(
        _snapshot_with_m15((*closed, forming)), candles_are_closed=False,
    )
    closed_context = MarketContextEngine().build(_snapshot_with_m15(closed))
    assert context.data_status.value == "READY"
    assert context.latest_m15_timestamp == closed[-1].timestamp
    assert context.m15_atr == closed_context.m15_atr
    assert context.m15_swing_points == closed_context.m15_swing_points
