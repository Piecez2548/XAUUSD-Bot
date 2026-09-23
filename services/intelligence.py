"""Deterministic Phase 3 market-context and strategy-intelligence pipeline.

The module is deliberately broker-independent.  It consumes a validated
``MarketSnapshot`` and existing strategy adapters, produces explainable
evidence, and never creates an execution request.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from sqlalchemy import select

from models.intelligence import (
    AlertDecision,
    CandidateState,
    ConfidenceBand,
    DataQualityStatus,
    Direction,
    EvidenceItem,
    EvidencePolarity,
    IntelligenceResult,
    ManualOBObservation,
    MarketContext,
    ScoreComponent,
    StrategyCandidate,
    SwingPoint,
    TrendDirection,
)
from models.market import Candle, MarketSnapshot, Timeframe
from models.shadow import ShadowAction
from services.features import FeatureValidationError, extract_features
from services.pair_zone_strategy import PairZoneV1


class ProvenanceConflictError(ValueError):
    """Raised when immutable intelligence provenance cannot be reconciled."""


INTELLIGENCE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "strategy_intelligence_v1.yaml"
SUPPORTED_INTELLIGENCE_VERSION = "phase3.0_intelligence_v1"
SCORING_CATEGORIES = (
    "STRUCTURE", "ZONE_CONTEXT", "ENTRY_CONTEXT", "MOMENTUM",
    "VOLATILITY", "SESSION", "CONTRADICTIONS",
)


@dataclass(frozen=True)
class IntelligenceContract:
    """Validated values from the versioned, reviewable Phase 3 contract."""

    intelligence_version: str
    context_version: str
    evidence_version: str
    scoring_version: str
    alert_policy_version: str
    scoring_weights: dict[str, float]
    confidence_thresholds: dict[str, float]
    alert_score_threshold: float
    alert_cooldown_minutes: float
    pivot_left_bars: int
    pivot_right_bars: int
    manual_ob_rejection_confirmation: str
    manual_ob_allowed_states: tuple[str, ...]


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"strategy intelligence config field {name!r} must be a mapping")
    return value


def load_intelligence_contract(path: Path = INTELLIGENCE_CONFIG_PATH) -> IntelligenceContract:
    """Load and fail closed on an incomplete or unsafe Phase 3 contract."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"unable to load strategy intelligence config: {path}") from exc
    config = _mapping(raw, "root")
    required = (
        "intelligence_version", "context_version", "evidence_version",
        "scoring_version", "alert_policy_version", "weights",
        "confidence_thresholds", "alert_score_threshold",
        "alert_cooldown_minutes", "pivot_left_bars", "pivot_right_bars",
        "required_timeframes", "manual_ob",
    )
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"strategy intelligence config missing fields: {', '.join(missing)}")
    if config["intelligence_version"] != SUPPORTED_INTELLIGENCE_VERSION:
        raise ValueError(f"unsupported strategy intelligence contract: {config['intelligence_version']}")
    if config.get("score_is_probability") is not False:
        raise ValueError("strategy intelligence score must not be treated as probability")
    if config.get("confidence_is_calibrated_probability") is not False:
        raise ValueError("strategy intelligence confidence must not be treated as calibrated probability")

    weights_raw = _mapping(config["weights"], "weights")
    if set(weights_raw) != set(SCORING_CATEGORIES):
        raise ValueError("strategy intelligence weights must cover exactly the scoring categories")
    weights = {key: float(weights_raw[key]) for key in SCORING_CATEGORIES}
    if any(value < 0 for value in weights.values()) or not 0.99 <= sum(weights.values()) <= 1.01:
        raise ValueError("strategy intelligence weights must be non-negative and sum to 1")

    thresholds_raw = _mapping(config["confidence_thresholds"], "confidence_thresholds")
    if not {"HIGH", "MODERATE"}.issubset(thresholds_raw):
        raise ValueError("strategy intelligence confidence thresholds are incomplete")
    thresholds = {key: float(value) for key, value in thresholds_raw.items()}
    if not 0 <= thresholds["MODERATE"] <= thresholds["HIGH"] <= 100:
        raise ValueError("strategy intelligence confidence thresholds are invalid")
    alert_score_threshold = float(config["alert_score_threshold"])
    if not 0 <= alert_score_threshold <= 100:
        raise ValueError("strategy intelligence alert score threshold is invalid")

    required_timeframes = tuple(str(value) for value in config["required_timeframes"])
    if not {"M15", "M5"}.issubset(required_timeframes):
        raise ValueError("strategy intelligence requires M15 and M5 timeframes")
    manual = _mapping(config["manual_ob"], "manual_ob")
    allowed_states = tuple(str(value) for value in manual.get("allowed_states", ()))
    if manual.get("rejection_confirmation") != "NOT_ACCEPTED":
        raise ValueError("manual M15 OB rejection confirmation must remain NOT_ACCEPTED")
    if not {"APPROACHING_OB", "ENTERED_OB"}.issubset(allowed_states):
        raise ValueError("manual M15 OB allowed states are incomplete")

    pivot_left = int(config["pivot_left_bars"])
    pivot_right = int(config["pivot_right_bars"])
    if pivot_left < 1 or pivot_right < 1:
        raise ValueError("pivot confirmation bars must be positive")
    cooldown = float(config["alert_cooldown_minutes"])
    if cooldown < 0:
        raise ValueError("alert cooldown must not be negative")
    return IntelligenceContract(
        intelligence_version=str(config["intelligence_version"]),
        context_version=str(config["context_version"]),
        evidence_version=str(config["evidence_version"]),
        scoring_version=str(config["scoring_version"]),
        alert_policy_version=str(config["alert_policy_version"]),
        scoring_weights=weights,
        confidence_thresholds=thresholds,
        alert_score_threshold=alert_score_threshold,
        alert_cooldown_minutes=cooldown,
        pivot_left_bars=pivot_left,
        pivot_right_bars=pivot_right,
        manual_ob_rejection_confirmation=str(manual["rejection_confirmation"]),
        manual_ob_allowed_states=allowed_states,
    )


INTELLIGENCE_CONTRACT = load_intelligence_contract()
INTELLIGENCE_VERSION = INTELLIGENCE_CONTRACT.intelligence_version
CONTEXT_VERSION = INTELLIGENCE_CONTRACT.context_version
EVIDENCE_VERSION = INTELLIGENCE_CONTRACT.evidence_version
SCORING_VERSION = INTELLIGENCE_CONTRACT.scoring_version
ALERT_POLICY_VERSION = INTELLIGENCE_CONTRACT.alert_policy_version
SCORING_WEIGHTS = INTELLIGENCE_CONTRACT.scoring_weights
CONFIDENCE_THRESHOLDS = INTELLIGENCE_CONTRACT.confidence_thresholds
ALERT_SCORE_THRESHOLD = INTELLIGENCE_CONTRACT.alert_score_threshold
ALERT_COOLDOWN = timedelta(minutes=INTELLIGENCE_CONTRACT.alert_cooldown_minutes)
PIVOT_LEFT_BARS = INTELLIGENCE_CONTRACT.pivot_left_bars
PIVOT_RIGHT_BARS = INTELLIGENCE_CONTRACT.pivot_right_bars
MIN_HISTORY = {Timeframe.M15: 8, Timeframe.M5: 20}
EXPECTED_INTERVAL = {Timeframe.M15: timedelta(minutes=15), Timeframe.M5: timedelta(minutes=5)}


def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _direction(value: ShadowAction | Direction | None) -> Direction:
    if value in (ShadowAction.BUY, Direction.BUY, "BUY"):
        return Direction.BUY
    if value in (ShadowAction.SELL, Direction.SELL, "SELL"):
        return Direction.SELL
    return Direction.NONE


def _trend(direction: Direction) -> TrendDirection:
    if direction == Direction.BUY:
        return TrendDirection.BULLISH
    if direction == Direction.SELL:
        return TrendDirection.BEARISH
    return TrendDirection.UNCLEAR


def _session(timestamp: datetime) -> str:
    hour = timestamp.astimezone(UTC).hour
    if 0 <= hour < 7:
        return "ASIA"
    if 7 <= hour < 12:
        return "LONDON"
    if 12 <= hour < 16:
        return "LONDON_NEW_YORK_OVERLAP"
    if 16 <= hour < 22:
        return "NEW_YORK"
    return "OFF_SESSION"


def _candidate_id(strategy: str, symbol: str, timestamp: datetime, direction: Direction, reference: str) -> str:
    key = "|".join((strategy, symbol, timestamp.astimezone(UTC).isoformat(), direction.value, reference))
    return "candidate-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _signed(item: EvidenceItem) -> float:
    if item.polarity == EvidencePolarity.BLOCKING:
        return -item.strength
    if item.polarity == EvidencePolarity.CONTRADICTORY:
        return -item.strength * abs(item.normalized_value or 1.0)
    if item.polarity == EvidencePolarity.SUPPORTIVE:
        return item.strength * item.normalized_value
    return 0.0


def _category(item: EvidenceItem) -> str:
    category_map = {
        "trend_structure": "STRUCTURE",
        "m15_alignment": "STRUCTURE",
        "pair_zone_state": "ZONE_CONTEXT",
        "manual_ob_state": "ZONE_CONTEXT",
        "m5_context": "ENTRY_CONTEXT",
        "displacement": "MOMENTUM",
        "volatility": "VOLATILITY",
        "session_context": "SESSION",
        "contradiction": "CONTRADICTIONS",
        "data_quality": "CONTRADICTIONS",
    }
    return category_map.get(item.type, "CONTRADICTIONS")


class MarketContextEngine:
    """Build causal M15/M5 context from closed candles only."""

    version = CONTEXT_VERSION

    def __init__(self, *, stale_after: timedelta = timedelta(minutes=30)) -> None:
        self.stale_after = stale_after

    @staticmethod
    def _closed_series(
        candles: tuple[Candle, ...], timeframe: Timeframe, as_of: datetime, candles_are_closed: bool
    ) -> tuple[Candle, ...]:
        values = tuple(candle for candle in candles if candle.timestamp <= as_of)
        if not candles_are_closed and values:
            values = values[:-1]
        if len(values) < MIN_HISTORY[timeframe]:
            raise FeatureValidationError(f"INSUFFICIENT_HISTORY_{timeframe.value}")
        timestamps = [candle.timestamp for candle in values]
        if any(left >= right for left, right in zip(timestamps, timestamps[1:], strict=False)):
            raise FeatureValidationError(f"UNSORTED_OR_DUPLICATE_{timeframe.value}")
        expected = EXPECTED_INTERVAL[timeframe]
        if any((right - left) != expected for left, right in zip(timestamps, timestamps[1:], strict=False)):
            raise FeatureValidationError(f"TIMEFRAME_GAP_{timeframe.value}")
        return values

    @staticmethod
    def _swings(candles: tuple[Candle, ...]) -> tuple[SwingPoint, ...]:
        points: list[SwingPoint] = []
        for index in range(PIVOT_LEFT_BARS, len(candles) - PIVOT_RIGHT_BARS):
            left = candles[index - PIVOT_LEFT_BARS : index]
            right = candles[index + 1 : index + 1 + PIVOT_RIGHT_BARS]
            candle = candles[index]
            if left and right and candle.high > max(item.high for item in left + right):
                points.append(
                    SwingPoint(
                        kind="SWING_HIGH", price=candle.high,
                        pivot_timestamp=candle.timestamp,
                        confirmed_at=right[-1].timestamp,
                        left_bars=PIVOT_LEFT_BARS, right_bars=PIVOT_RIGHT_BARS,
                    )
                )
            if left and right and candle.low < min(item.low for item in left + right):
                points.append(
                    SwingPoint(
                        kind="SWING_LOW", price=candle.low,
                        pivot_timestamp=candle.timestamp,
                        confirmed_at=right[-1].timestamp,
                        left_bars=PIVOT_LEFT_BARS, right_bars=PIVOT_RIGHT_BARS,
                    )
                )
        return tuple(sorted(points, key=lambda point: point.confirmed_at))

    @staticmethod
    def _structure(points: tuple[SwingPoint, ...]) -> tuple[str, TrendDirection]:
        highs = [point.price for point in points if point.kind == "SWING_HIGH"]
        lows = [point.price for point in points if point.kind == "SWING_LOW"]
        if len(highs) < 2 or len(lows) < 2:
            return "INSUFFICIENT", TrendDirection.UNCLEAR
        higher_high = highs[-1] > highs[-2]
        higher_low = lows[-1] > lows[-2]
        lower_high = highs[-1] < highs[-2]
        lower_low = lows[-1] < lows[-2]
        if higher_high and higher_low:
            return "HH_HL", TrendDirection.BULLISH
        if lower_high and lower_low:
            return "LH_LL", TrendDirection.BEARISH
        return "MIXED", TrendDirection.RANGE

    def build(
        self,
        snapshot: MarketSnapshot,
        *,
        as_of: datetime | None = None,
        candles_are_closed: bool = True,
    ) -> MarketContext:
        as_of = as_of or snapshot.generated_at
        if as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        try:
            m15 = self._closed_series(snapshot.candles[Timeframe.M15], Timeframe.M15, as_of, candles_are_closed)
            m5 = self._closed_series(snapshot.candles[Timeframe.M5], Timeframe.M5, as_of, candles_are_closed)
        except (KeyError, FeatureValidationError) as exc:
            return MarketContext(
                symbol=snapshot.symbol.name, as_of=as_of, version=self.version,
                data_status=DataQualityStatus.INSUFFICIENT_DATA,
                data_reasons=(str(exc),), session=_session(as_of),
            )
        latest = max(m5[-1].timestamp, m15[-1].timestamp)
        if as_of - latest > self.stale_after:
            status = DataQualityStatus.STALE
            reasons = ("STALE_CLOSED_CANDLES",)
        else:
            status = DataQualityStatus.READY
            reasons = ()
        m15_points = self._swings(m15)
        m5_points = self._swings(m5)
        m15_structure, m15_trend = self._structure(m15_points)
        m5_structure, m5_trend = self._structure(m5_points)
        try:
            m15_features = extract_features(m15, timeframe="M15", spread=snapshot.symbol.spread, forming_last=False)
            m5_features = extract_features(m5, timeframe="M5", spread=snapshot.symbol.spread, forming_last=False)
        except FeatureValidationError as exc:
            return MarketContext(
                symbol=snapshot.symbol.name, as_of=as_of, version=self.version,
                data_status=DataQualityStatus.INVALID, data_reasons=(str(exc),),
                session=_session(as_of), latest_m15_timestamp=m15[-1].timestamp,
                latest_m5_timestamp=m5[-1].timestamp,
            )
        if m15_trend == m5_trend and m15_trend in {TrendDirection.BULLISH, TrendDirection.BEARISH}:
            trend = m15_trend
        elif m15_trend == TrendDirection.RANGE and m5_trend == TrendDirection.RANGE:
            trend = TrendDirection.RANGE
        else:
            trend = TrendDirection.UNCLEAR
        return MarketContext(
            symbol=snapshot.symbol.name, as_of=as_of, version=self.version,
            data_status=status, data_reasons=reasons, trend=trend,
            m15_trend=m15_trend, m5_trend=m5_trend,
            m15_structure=m15_structure, m5_structure=m5_structure,
            m15_swing_points=m15_points[-8:], m5_swing_points=m5_points[-8:],
            m15_atr=m15_features.atr, m5_atr=m5_features.atr,
            m15_displacement=m15_features.body_size / m15_features.atr if m15_features.atr else None,
            m5_displacement=m5_features.body_size / m5_features.atr if m5_features.atr else None,
            m5_relative_volatility=m5_features.relative_volatility,
            session=_session(as_of), latest_m15_timestamp=m15[-1].timestamp,
            latest_m5_timestamp=m5[-1].timestamp,
        )


class EvidenceScorer:
    """Convert explicit evidence into a bounded descriptive score."""

    version = SCORING_VERSION

    def score(
        self,
        evidence: tuple[EvidenceItem, ...],
        *,
        blockers: tuple[str, ...] = (),
    ) -> tuple[float, ConfidenceBand, tuple[ScoreComponent, ...]]:
        components: list[ScoreComponent] = []
        total = 50.0
        for category, weight in SCORING_WEIGHTS.items():
            items = tuple(item for item in evidence if _category(item) == category)
            if not items:
                components.append(ScoreComponent(
                    category=category, raw_evidence=(), normalized_contribution=0.0,
                    weight=weight, reason="No evidence collected for this category.",
                ))
                continue
            signed = sum(_signed(item) for item in items) / len(items)
            contribution = weight * _clamp(signed) * 100
            total += contribution
            components.append(ScoreComponent(
                category=category,
                raw_evidence=tuple(item.rule_id for item in items),
                normalized_contribution=round(contribution, 4), weight=weight,
                reason="; ".join(item.type for item in items),
            ))
        score = round(max(0.0, min(100.0, total)), 2)
        if score >= CONFIDENCE_THRESHOLDS["HIGH"]:
            band = ConfidenceBand.HIGH
        elif score >= CONFIDENCE_THRESHOLDS["MODERATE"]:
            band = ConfidenceBand.MODERATE
        else:
            band = ConfidenceBand.LOW
        return score, band, tuple(components)


class AlertPolicy:
    """State-transition-aware alert policy; it never emits broker actions."""

    version = ALERT_POLICY_VERSION

    def decide(self, candidate: StrategyCandidate, previous: StrategyCandidate | None = None) -> AlertDecision:
        if candidate.blockers:
            return AlertDecision.BLOCK
        if candidate.state in {CandidateState.OBSERVING, CandidateState.EXPIRED}:
            return AlertDecision.IGNORE
        if candidate.score < ALERT_SCORE_THRESHOLD:
            return AlertDecision.OBSERVE
        if previous is not None:
            same_state = previous.state == candidate.state and previous.direction == candidate.direction
            within_cooldown = candidate.detected_at - previous.detected_at < ALERT_COOLDOWN
            if same_state and within_cooldown:
                return AlertDecision.OBSERVE
        return AlertDecision.ALERT


class ExactPairAdapter:
    """Adapter around Pair Zone V1; it does not alter Pair Zone rules."""

    strategy = "pair_zone_v1"

    def __init__(self, settings) -> None:
        self.detector = PairZoneV1(settings)

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        context: MarketContext,
        *,
        risk: Any | None = None,
        previous: StrategyCandidate | None = None,
    ) -> StrategyCandidate:
        timestamp = context.latest_m5_timestamp or context.as_of
        evidence: list[EvidenceItem] = []
        blockers = list(context.data_reasons if context.data_status != DataQualityStatus.READY else ())
        if context.data_status != DataQualityStatus.READY:
            evidence.append(EvidenceItem(
                type="data_quality", source="market_context", timeframe="M5/M15",
                value=context.data_status.value, normalized_value=-1.0,
                polarity=EvidencePolarity.BLOCKING, strength=1.0,
                timestamp=timestamp, rule_id="DATA_QUALITY_BLOCK",
            ))
            state = CandidateState.OBSERVING
            direction = Direction.NONE
            reference = "no-data"
            strategy_reason = "INSUFFICIENT_DATA"
        else:
            decision = self.detector.evaluate(
                snapshot, risk=risk, data_freshness="LIVE", mt5_state="CONNECTED",
                runtime_state="CONNECTED", candles_are_closed=True,
            )
            direction = _direction(decision.decision)
            reason = decision.reason_codes[0] if decision.reason_codes else "UNKNOWN"
            context_direction = Direction.BUY if context.trend == TrendDirection.BULLISH else Direction.SELL if context.trend == TrendDirection.BEARISH else Direction.NONE
            if context_direction != Direction.NONE and direction != Direction.NONE and context_direction == direction:
                evidence.append(EvidenceItem(
                    type="trend_structure", source="market_context", timeframe="M15/M5",
                    value=context.trend.value, normalized_value=1.0,
                    polarity=EvidencePolarity.SUPPORTIVE, strength=0.9,
                    timestamp=timestamp, rule_id="STRUCTURE_ALIGNMENT",
                ))
            elif context_direction != Direction.NONE and direction != Direction.NONE:
                evidence.append(EvidenceItem(
                    type="contradiction", source="market_context", timeframe="M15/M5",
                    value=context.trend.value, normalized_value=-1.0,
                    polarity=EvidencePolarity.CONTRADICTORY, strength=0.8,
                    timestamp=timestamp, rule_id="STRUCTURE_CONTRADICTION",
                ))
                blockers.append("STRUCTURE_CONTRADICTION")
            else:
                evidence.append(EvidenceItem(
                    type="trend_structure", source="market_context", timeframe="M15/M5",
                    value=context.trend.value, normalized_value=0.0,
                    polarity=EvidencePolarity.NEUTRAL, strength=0.4,
                    timestamp=timestamp, rule_id="STRUCTURE_UNCLEAR",
                ))
            pair_state = str(decision.feature_context.get("zone_state", "NONE"))
            if direction != Direction.NONE:
                state = CandidateState.CONFIRMED
                pair_polarity = EvidencePolarity.SUPPORTIVE
                pair_value = 1.0
            elif reason == "ZONE_CONFIRMATION_MISSING":
                state = CandidateState.IN_ZONE
                pair_polarity = EvidencePolarity.NEUTRAL
                pair_value = 0.25
            elif reason == "HTF_DIRECTION_MISMATCH":
                state = CandidateState.CANDIDATE
                pair_polarity = EvidencePolarity.CONTRADICTORY
                pair_value = -0.8
                blockers.append("HTF_DIRECTION_MISMATCH")
            elif reason in {"ZONE_INVALIDATED", "INVALID_SL"}:
                state = CandidateState.INVALIDATED
                pair_polarity = EvidencePolarity.BLOCKING
                pair_value = -1.0
                blockers.append(reason)
            else:
                state = CandidateState.OBSERVING
                pair_polarity = EvidencePolarity.NEUTRAL
                pair_value = 0.0
            evidence.append(EvidenceItem(
                type="pair_zone_state", source="pair_zone_v1", timeframe="M15/M5",
                value=pair_state or reason, normalized_value=pair_value,
                polarity=pair_polarity, strength=1.0 if direction != Direction.NONE else 0.7,
                timestamp=timestamp, rule_id=f"PAIR_ZONE_{reason}",
                metadata={"reason_code": reason, "strategy_version": self.detector.strategy_version},
            ))
            displacement = context.m5_displacement
            evidence.append(EvidenceItem(
                type="displacement", source="market_context", timeframe="M5",
                value=displacement, normalized_value=_clamp((displacement or 0.0) / 2.0),
                polarity=EvidencePolarity.SUPPORTIVE if displacement and displacement >= 1 else EvidencePolarity.NEUTRAL,
                strength=0.5, timestamp=timestamp, rule_id="M5_DISPLACEMENT",
            ))
            reference = str(decision.feature_context.get("zone_id", reason))
            strategy_reason = reason
        score, band, components = EvidenceScorer().score(tuple(evidence), blockers=tuple(blockers))
        candidate = StrategyCandidate(
            candidate_id=_candidate_id(self.strategy, snapshot.symbol.name, timestamp, direction, reference),
            symbol=snapshot.symbol.name, strategy=self.strategy,
            strategy_version=self.detector.strategy_version, direction=direction,
            timeframe="M15/M5", detected_at=timestamp, context_reference=reference,
            state=state, evidence=tuple(evidence), score=score,
            confidence_band=band, blockers=tuple(dict.fromkeys(blockers)),
            warnings=(strategy_reason,), score_components=components,
            source="ExactPairAdapter", evidence_version=EVIDENCE_VERSION,
            alert_decision=AlertDecision.IGNORE, execution_allowed=False,
        )
        decision = AlertPolicy().decide(candidate, previous)
        return candidate.model_copy(update={"alert_decision": decision})


class ManualOBAdapter:
    """Conservative adapter for future/manual M15 OB observations."""

    strategy = "manual_m15_ob"

    def adapt(
        self,
        observation: ManualOBObservation,
        context: MarketContext,
        *,
        previous: StrategyCandidate | None = None,
    ) -> StrategyCandidate:
        state_map = {"APPROACHING_OB": CandidateState.APPROACHING, "ENTERED_OB": CandidateState.IN_ZONE,
                     "POTENTIAL_REJECTION": CandidateState.CANDIDATE}
        state = state_map.get(observation.state, CandidateState.OBSERVING)
        evidence = [EvidenceItem(
            type="manual_ob_state", source="manual_observation", timeframe=observation.timeframe,
            value=observation.state, normalized_value=1.0 if state != CandidateState.OBSERVING else 0.0,
            polarity=EvidencePolarity.SUPPORTIVE if state != CandidateState.OBSERVING else EvidencePolarity.NEUTRAL,
            strength=0.7, timestamp=observation.observed_at,
            rule_id="MANUAL_OB_OBSERVATION", metadata={"observation_id": observation.observation_id},
        )]
        warnings = ("MANUAL_REJECTION_NOT_ACCEPTED", "REVIEW_REQUIRED")
        score, band, components = EvidenceScorer().score(tuple(evidence))
        candidate = StrategyCandidate(
            candidate_id=_candidate_id(self.strategy, observation.symbol, observation.observed_at,
                                        observation.direction, observation.zone_reference or observation.observation_id),
            symbol=observation.symbol, strategy=self.strategy, strategy_version="manual_ob_adapter_v1",
            direction=observation.direction, timeframe=observation.timeframe,
            detected_at=observation.observed_at, context_reference=observation.zone_reference or "manual",
            state=state, evidence=tuple(evidence), score=score, confidence_band=band,
            blockers=(), warnings=warnings, score_components=components,
            alert_decision=AlertDecision.OBSERVE, source="ManualOBAdapter",
            evidence_version=EVIDENCE_VERSION, execution_allowed=False,
        )
        if previous is not None and previous.detected_at == candidate.detected_at:
            return candidate.model_copy(update={"alert_decision": AlertDecision.OBSERVE})
        return candidate


class StrategyIntelligenceEngine:
    """Pipeline facade: context -> adapter -> evidence score -> alert policy."""

    version = INTELLIGENCE_VERSION

    def __init__(self, settings, *, adapter: ExactPairAdapter | None = None) -> None:
        self.context_engine = MarketContextEngine()
        self.adapter = adapter or ExactPairAdapter(settings)

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        *,
        risk: Any | None = None,
        as_of: datetime | None = None,
        previous: StrategyCandidate | None = None,
        candles_are_closed: bool = True,
    ) -> IntelligenceResult:
        context = self.context_engine.build(snapshot, as_of=as_of, candles_are_closed=candles_are_closed)
        candidate = self.adapter.evaluate(snapshot, context, risk=risk, previous=previous)
        return IntelligenceResult(context=context, candidate=candidate)


def serialize_for_forward_shadow(result: IntelligenceResult) -> dict[str, Any]:
    """Return an immutable JSON-compatible snapshot for future outcomes."""

    return {
        "intelligence_version": INTELLIGENCE_VERSION,
        "context_version": result.context.version,
        "evidence_version": result.candidate.evidence_version,
        "candidate": result.candidate.model_dump(mode="json"),
        "context": result.context.model_dump(mode="json"),
        "execution_allowed": False,
    }


def persist_intelligence_record(
    database: Any,
    result: IntelligenceResult,
    *,
    forward_session_id: str | None = None,
    forward_signal_id: str | None = None,
    pair_zone_event_id: str | None = None,
) -> Any:
    """Persist one idempotent evidence snapshot without changing strategy history.

    Forward linkage is intentionally not late-bound by this function.  Use
    :func:`link_intelligence_forward_provenance` for the explicit, auditable
    lifecycle link after a Forward Signal exists.
    """

    from persistence.orm import StrategyIntelligenceRecord

    candidate = result.candidate
    payload = candidate.model_dump(mode="json")
    with database.session() as session:
        existing = session.scalar(
            select(StrategyIntelligenceRecord).where(
                StrategyIntelligenceRecord.candidate_id == candidate.candidate_id
            )
        )
        if existing is not None:
            immutable = (
                ("symbol", existing.symbol, candidate.symbol),
                ("strategy", existing.strategy, candidate.strategy),
                ("strategy_version", existing.strategy_version, candidate.strategy_version),
                ("evidence_version", existing.evidence_version, candidate.evidence_version),
                ("detected_at", existing.detected_at, candidate.detected_at),
            )
            for field_name, current, incoming in immutable:
                if current != incoming:
                    raise ProvenanceConflictError(
                        f"candidate {candidate.candidate_id} immutable field conflict: {field_name}"
                    )
            for field_name, incoming in (
                ("forward_session_id", forward_session_id),
                ("forward_signal_id", forward_signal_id),
                ("pair_zone_event_id", pair_zone_event_id),
            ):
                current = getattr(existing, field_name)
                if current is not None and incoming is not None and current != incoming:
                    raise ProvenanceConflictError(
                        f"candidate {candidate.candidate_id} provenance conflict: {field_name}"
                    )
            return existing
        row = StrategyIntelligenceRecord(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            strategy=candidate.strategy,
            strategy_version=candidate.strategy_version,
            intelligence_version=INTELLIGENCE_VERSION,
            intelligence_runtime_version=StrategyIntelligenceEngine.version,
            evidence_version=candidate.evidence_version,
            detected_at=candidate.detected_at,
            timeframe=candidate.timeframe,
            direction=candidate.direction.value,
            state=candidate.state.value,
            score=candidate.score,
            confidence_band=candidate.confidence_band.value,
            alert_decision=candidate.alert_decision.value,
            blockers_json=list(candidate.blockers),
            warnings_json=list(candidate.warnings),
            context_json=result.context.model_dump(mode="json"),
            evidence_json=payload["evidence"],
            score_components_json=payload["score_components"],
            source=candidate.source,
            pair_zone_event_id=pair_zone_event_id,
            forward_session_id=forward_session_id,
            forward_signal_id=forward_signal_id,
            execution_allowed=False,
        )
        session.add(row)
        session.flush()
        return row


def link_intelligence_forward_provenance(
    database: Any,
    *,
    candidate_id: str,
    forward_session_id: str,
    forward_signal_id: str,
) -> Any:
    """Bind one candidate to exactly one same-session Forward Signal/trade.

    The signal and its virtual trade are already created by Forward Shadow.
    This function permits only the modeled late-bound lifecycle fields to be
    filled once.  It never changes the candidate evidence or decision.
    """

    from persistence.orm import (
        ForwardSignalRecord,
        ForwardTradeRecord,
        ForwardValidationSessionRecord,
        StrategyIntelligenceRecord,
    )

    with database.session() as session:
        candidate = session.scalar(
            select(StrategyIntelligenceRecord).where(
                StrategyIntelligenceRecord.candidate_id == candidate_id
            )
        )
        if candidate is None:
            raise ProvenanceConflictError(f"intelligence candidate not found: {candidate_id}")
        signal = session.get(ForwardSignalRecord, forward_signal_id)
        if signal is None:
            raise ProvenanceConflictError(f"forward signal not found: {forward_signal_id}")
        forward_session = session.get(ForwardValidationSessionRecord, forward_session_id)
        if forward_session is None:
            raise ProvenanceConflictError(f"forward session not found: {forward_session_id}")
        if signal.session_id != forward_session.id:
            raise ProvenanceConflictError("forward signal belongs to a different session")
        if candidate.forward_session_id not in (None, forward_session.id):
            raise ProvenanceConflictError("candidate is already linked to a different session")
        if candidate.forward_signal_id not in (None, signal.id):
            raise ProvenanceConflictError("candidate is already linked to a different signal")

        trade = session.scalar(
            select(ForwardTradeRecord).where(ForwardTradeRecord.signal_id == signal.id)
        )
        if trade is not None:
            if trade.session_id != forward_session.id:
                raise ProvenanceConflictError("forward trade belongs to a different session")
            if trade.terminal_timestamp is not None and trade.terminal_timestamp <= candidate.detected_at:
                raise ProvenanceConflictError("forward outcome is not after the intelligence observation")
            if candidate.forward_trade_id not in (None, trade.id):
                raise ProvenanceConflictError("candidate is already linked to a different outcome")
            candidate.forward_trade_id = trade.id

        candidate.forward_session_id = forward_session.id
        candidate.forward_signal_id = signal.id
        session.flush()
        return candidate
