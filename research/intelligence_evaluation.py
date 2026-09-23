"""Deterministic, offline evaluation of Strategy Intelligence evidence.

This module is intentionally separate from runtime and execution code.  It
accepts provenance-bearing observations, joins outcomes only by an explicit
stable identifier, and returns N/A when a denominator or calibrated label is
not available.  It never writes operational, Forward Shadow, or broker state.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVALUATION_VERSION = "phase3.1_intelligence_evaluation_v1"
RUNTIME_VERSION = "phase3.0_intelligence_v1"
POSITIVE_OUTCOMES = frozenset({"TP_HIT", "TP", "WIN", "POSITIVE"})
NEGATIVE_OUTCOMES = frozenset({"SL_HIT", "SL", "LOSS", "NEGATIVE"})
UNRESOLVED_OUTCOMES = frozenset({"PENDING", "AMBIGUOUS", "UNRESOLVED", "UNKNOWN", "INVALID"})
MANUAL_OB_ALLOWED = frozenset({"APPROACHING_OB", "ENTERED_OB"})
MANUAL_OB_REJECTION = "REJECTION_CONFIRMED"


class EvaluationInputError(ValueError):
    """Raised internally when one observation cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """One deterministic evaluation unit with explicit provenance.

    ``observation_id`` must be the canonical candidate/event identifier.  The
    evaluator never joins records by timestamp.  Timestamps are used only for
    ordering and leakage checks.
    """

    observation_id: str
    symbol: str
    timeframe: str
    timestamp: datetime
    pair_zone_source: Mapping[str, Any] = field(default_factory=dict)
    intelligence_version: str = RUNTIME_VERSION
    intelligence_runtime_version: str = RUNTIME_VERSION
    evidence_version: str | None = None
    score: float | None = None
    confidence: float | None = None
    confidence_band: str | None = None
    disposition: str | None = None
    state: str | None = None
    direction: str | None = None
    evidence: tuple[Mapping[str, Any], ...] = ()
    score_components: tuple[Mapping[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)
    manual_ob_state: str | None = None
    baseline_positive: bool | None = None
    outcome_status: str | None = None
    outcome_realized_r: float | None = None
    outcome_timestamp: datetime | None = None
    execution_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.observation_id or not self.symbol or not self.timeframe:
            raise EvaluationInputError("observation provenance is incomplete")
        if self.timestamp.tzinfo is None:
            raise EvaluationInputError("observation timestamp must be timezone-aware")
        if self.outcome_timestamp is not None:
            if self.outcome_timestamp.tzinfo is None:
                raise EvaluationInputError("outcome timestamp must be timezone-aware")
            if self.outcome_timestamp <= self.timestamp:
                raise EvaluationInputError("outcome timestamp must be after observation timestamp")
        if self.execution_allowed is not False:
            raise EvaluationInputError("evaluation input cannot enable execution")
        if self.score is not None and (not math.isfinite(self.score) or not 0 <= self.score <= 100):
            raise EvaluationInputError("score must be finite and within 0..100")
        if self.confidence is not None and not math.isfinite(self.confidence):
            raise EvaluationInputError("confidence must be finite")
        if self.manual_ob_state == MANUAL_OB_REJECTION:
            raise EvaluationInputError("manual M15 OB rejection confirmation is not accepted")
        if self.manual_ob_state is not None and self.manual_ob_state not in MANUAL_OB_ALLOWED:
            raise EvaluationInputError(f"unsupported manual M15 OB state: {self.manual_ob_state}")
        for item in self.evidence:
            if not isinstance(item.get("type", ""), str):
                raise EvaluationInputError("evidence type must be a string")
            value = item.get("timestamp")
            if value is None:
                continue
            evidence_timestamp = _timestamp(value)
            if evidence_timestamp > self.timestamp:
                raise EvaluationInputError("future evidence is not allowed")
        for component in self.score_components:
            if not isinstance(component.get("category"), str) or not component.get("category"):
                raise EvaluationInputError("score component category is required")
            raw_evidence = component.get("raw_evidence", ())
            if not isinstance(raw_evidence, (list, tuple)) or any(
                not isinstance(value, str) for value in raw_evidence
            ):
                raise EvaluationInputError("score component raw_evidence must be a list of strings")
            contribution = _optional_float(
                component.get("normalized_contribution", 0.0), "component contribution"
            )
            if contribution is None:
                raise EvaluationInputError("component contribution is required")
        if self.baseline_positive is not None and not isinstance(self.baseline_positive, bool):
            raise EvaluationInputError("baseline_positive must be boolean or null")
        for key in ("as_of", "latest_m15_timestamp", "latest_m5_timestamp"):
            value = self.context.get(key)
            if value is not None and _timestamp(value) > self.timestamp:
                raise EvaluationInputError("future context is not allowed")


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise EvaluationInputError("timestamp must be an ISO string or datetime")
    if result.tzinfo is None:
        raise EvaluationInputError("timestamp must be timezone-aware")
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EvaluationInputError(f"{name} must be an object")
    return dict(value)


def _mapping_tuple(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, Mapping) for item in value):
        raise EvaluationInputError(f"{name} must be a list of objects")
    return tuple(dict(item) for item in value)


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise EvaluationInputError(f"{name} must be a list of strings")
    return tuple(value)


def _optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationInputError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise EvaluationInputError(f"{name} must be finite")
    return result


def _manual_state(evidence: Sequence[Mapping[str, Any]]) -> str | None:
    for item in evidence:
        if item.get("type") == "manual_ob_state":
            value = item.get("value")
            return str(value) if value is not None else None
    return None


def observation_from_mapping(raw: Mapping[str, Any]) -> EvaluationObservation:
    """Convert a persisted or fixture mapping without timestamp matching."""

    if not isinstance(raw, Mapping):
        raise EvaluationInputError("observation must be an object")
    evidence = _mapping_tuple(raw.get("evidence"), "evidence")
    manual_state = raw.get("manual_ob_state") or _manual_state(evidence)
    return EvaluationObservation(
        observation_id=str(raw.get("observation_id") or raw.get("candidate_id") or ""),
        symbol=str(raw.get("symbol") or ""),
        timeframe=str(raw.get("timeframe") or ""),
        timestamp=_timestamp(raw.get("timestamp") or raw.get("detected_at")),
        pair_zone_source=_mapping(raw.get("pair_zone_source"), "pair_zone_source"),
        intelligence_version=str(raw.get("intelligence_version") or RUNTIME_VERSION),
        intelligence_runtime_version=str(
            raw.get("intelligence_runtime_version") or RUNTIME_VERSION
        ),
        evidence_version=str(raw["evidence_version"])
        if raw.get("evidence_version") is not None
        else None,
        score=_optional_float(raw.get("score"), "score"),
        confidence=_optional_float(raw.get("confidence"), "confidence"),
        confidence_band=str(raw["confidence_band"])
        if raw.get("confidence_band") is not None
        else None,
        disposition=str(raw["disposition"]) if raw.get("disposition") is not None else None,
        state=str(raw["state"]) if raw.get("state") is not None else None,
        direction=str(raw["direction"]) if raw.get("direction") is not None else None,
        evidence=evidence,
        score_components=_mapping_tuple(raw.get("score_components"), "score_components"),
        blockers=_string_tuple(raw.get("blockers"), "blockers"),
        warnings=_string_tuple(raw.get("warnings"), "warnings"),
        context=_mapping(raw.get("context"), "context"),
        manual_ob_state=str(manual_state) if manual_state is not None else None,
        baseline_positive=raw.get("baseline_positive"),
        outcome_status=str(raw["outcome_status"])
        if raw.get("outcome_status") is not None
        else None,
        outcome_realized_r=_optional_float(raw.get("outcome_realized_r"), "outcome_realized_r"),
        outcome_timestamp=_timestamp(raw["outcome_timestamp"])
        if raw.get("outcome_timestamp")
        else None,
        execution_allowed=raw.get("execution_allowed", False),
    )


def _outcome_class(observation: EvaluationObservation) -> str:
    status = (observation.outcome_status or "").upper()
    if status in POSITIVE_OUTCOMES:
        return "POSITIVE"
    if status in NEGATIVE_OUTCOMES:
        return "NEGATIVE"
    if status == "EXPIRED" and observation.outcome_realized_r is not None:
        return "POSITIVE" if observation.outcome_realized_r > 0 else "NEGATIVE"
    return "UNRESOLVED"


def _na(reason: str) -> dict[str, Any]:
    return {"value": None, "status": "N/A", "reason": reason}


def _rate(numerator: int, denominator: int, reason: str) -> dict[str, Any]:
    if denominator <= 0:
        return _na(reason)
    return {"value": round(numerator / denominator, 4), "status": "MEASURED", "n": denominator}


def _mean(values: Sequence[float], reason: str) -> dict[str, Any]:
    if not values:
        return _na(reason)
    return {"value": round(sum(values) / len(values), 4), "status": "MEASURED", "n": len(values)}


def _counts(observations: Sequence[EvaluationObservation]) -> dict[str, int]:
    labels = Counter(_outcome_class(item) for item in observations)
    return {
        "observation_count": len(observations),
        "resolved_count": labels["POSITIVE"] + labels["NEGATIVE"],
        "positive_count": labels["POSITIVE"],
        "negative_count": labels["NEGATIVE"],
        "unresolved_count": labels["UNRESOLVED"],
    }


def _dataset_metrics(observations: Sequence[EvaluationObservation]) -> dict[str, Any]:
    counts = _counts(observations)
    resolved = counts["resolved_count"]
    return {
        **counts,
        "coverage": _rate(resolved, counts["observation_count"], "no observations"),
        "outcome_rate": _rate(counts["positive_count"], resolved, "no resolved outcome labels"),
        "unresolved_treatment": (
            "excluded from outcome rates and classifier metrics; counted separately"
        ),
    }


def _classifier_metrics(
    observations: Sequence[EvaluationObservation],
    predictor: Any,
    *,
    unavailable_reason: str,
) -> dict[str, Any]:
    labeled = [
        (item, predictor(item)) for item in observations if _outcome_class(item) != "UNRESOLVED"
    ]
    labeled = [(item, prediction) for item, prediction in labeled if prediction is not None]
    if not labeled:
        return {
            "observation_count": len(observations),
            "labeled_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "unresolved_count": _counts(observations)["unresolved_count"],
            "coverage": _rate(
                _counts(observations)["resolved_count"], len(observations), "no observations"
            ),
            "outcome_rate": _rate(0, 0, unavailable_reason),
            "false_positive_rate": _na(unavailable_reason),
            "precision": _na(unavailable_reason),
            "recall": _na(unavailable_reason),
            "f1": _na(unavailable_reason),
            "confusion": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
        }
    tp = fp = tn = fn = 0
    for item, prediction in labeled:
        actual_positive = _outcome_class(item) == "POSITIVE"
        if bool(prediction) and actual_positive:
            tp += 1
        elif bool(prediction) and not actual_positive:
            fp += 1
        elif not bool(prediction) and actual_positive:
            fn += 1
        else:
            tn += 1
    resolved = len(labeled)
    return {
        "observation_count": len(observations),
        "labeled_count": resolved,
        "positive_count": sum(_outcome_class(item) == "POSITIVE" for item, _ in labeled),
        "negative_count": sum(_outcome_class(item) == "NEGATIVE" for item, _ in labeled),
        "unresolved_count": _counts(observations)["unresolved_count"],
        "coverage": _rate(
            _counts(observations)["resolved_count"], len(observations), "no observations"
        ),
        "outcome_rate": _rate(tp + fn, resolved, "no resolved outcome labels"),
        "false_positive_rate": _rate(fp, fp + tn, "no negative outcome labels"),
        "precision": _rate(tp, tp + fp, "no positive predictions"),
        "recall": _rate(tp, tp + fn, "no positive outcome labels"),
        "f1": _rate(2 * tp, 2 * tp + fp + fn, "no positive predictions or labels"),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def _score_bucket(score: float | None, moderate: float, high: float) -> str:
    if score is None:
        return "MISSING"
    if score >= high:
        return "HIGH"
    if score >= moderate:
        return "MODERATE"
    return "LOW"


def _segmented_metrics(observations: Sequence[EvaluationObservation]) -> dict[str, Any]:
    try:
        from services.intelligence import CONFIDENCE_THRESHOLDS

        moderate = CONFIDENCE_THRESHOLDS["MODERATE"]
        high = CONFIDENCE_THRESHOLDS["HIGH"]
    except Exception:
        moderate, high = 45.0, 70.0
    buckets: dict[str, list[EvaluationObservation]] = defaultdict(list)
    for item in observations:
        buckets[_score_bucket(item.score, moderate, high)].append(item)
    output: dict[str, Any] = {
        "contract_boundaries": {"LOW_LT": moderate, "MODERATE_GTE": moderate, "HIGH_GTE": high},
        "buckets": [],
        "calibration": _na(
            "score and confidence are descriptive contract fields, not calibrated probabilities"
        ),
    }
    for name in ("LOW", "MODERATE", "HIGH", "MISSING"):
        members = buckets.get(name, [])
        counts = _counts(members)
        confidence_values = [item.confidence for item in members if item.confidence is not None]
        output["buckets"].append(
            {
                "bucket": name,
                **counts,
                "coverage": _rate(
                    counts["resolved_count"], counts["observation_count"], "no observations"
                ),
                "positive_rate": _rate(
                    counts["positive_count"], counts["resolved_count"], "no resolved labels"
                ),
                "negative_rate": _rate(
                    counts["negative_count"], counts["resolved_count"], "no resolved labels"
                ),
                "confidence_mean": _mean(
                    confidence_values, "confidence is categorical or unavailable"
                ),
                "observed_outcome_rate": _rate(
                    counts["positive_count"], counts["resolved_count"], "no resolved labels"
                ),
            }
        )
    return output


def _component_entries(item: EvaluationObservation) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for component in item.score_components:
        category = component.get("category")
        if isinstance(category, str) and category:
            result[category] = component
    return result


def _component_metrics(observations: Sequence[EvaluationObservation]) -> dict[str, Any]:
    categories = sorted(
        {category for item in observations for category in _component_entries(item)}
    )
    result: dict[str, Any] = {}
    for category in categories:
        present: list[EvaluationObservation] = []
        disagreement = 0
        disagreement_denominator = 0
        for item in observations:
            components = _component_entries(item)
            component = components.get(category)
            if component is None:
                continue
            raw = component.get("raw_evidence")
            if isinstance(raw, list) and raw:
                present.append(item)
            contribution = _optional_float(
                component.get("normalized_contribution"), "component contribution"
            )
            other = sum(
                float(value.get("normalized_contribution", 0.0) or 0.0)
                for key, value in components.items()
                if key != category and isinstance(value, Mapping)
            )
            if contribution is not None and contribution != 0 and other != 0:
                disagreement_denominator += 1
                if (contribution > 0) != (other > 0):
                    disagreement += 1
        resolved_present = [item for item in present if _outcome_class(item) != "UNRESOLVED"]
        labels = Counter(_outcome_class(item) for item in resolved_present)
        n = len(observations)
        result[category] = {
            "sample_size": n,
            "present_count": len(present),
            "missing_count": n - len(present),
            "coverage": _rate(len(present), n, "no observations"),
            "missing_rate": _rate(n - len(present), n, "no observations"),
            "resolved_present_count": len(resolved_present),
            "positive_count": labels["POSITIVE"],
            "negative_count": labels["NEGATIVE"],
            "outcome_association": _rate(
                labels["POSITIVE"],
                len(resolved_present),
                "no resolved labels with component present",
            ),
            "disagreement_rate": _rate(
                disagreement, disagreement_denominator, "no comparable component evidence"
            ),
            "information_value": (
                "INSUFFICIENT_LABELS"
                if len(resolved_present) < 10
                else "OBSERVED_ASSOCIATION_ONLY_NOT_CAUSAL"
            ),
        }
    return result


def _manual_ob_metrics(observations: Sequence[EvaluationObservation]) -> dict[str, Any]:
    manual = [item for item in observations if item.manual_ob_state is not None]
    if not manual:
        return {"status": "N/A", "reason": "no manual M15 OB observations present", "states": {}}
    states: dict[str, Any] = {}
    for state in sorted({item.manual_ob_state for item in manual if item.manual_ob_state}):
        members = [item for item in manual if item.manual_ob_state == state]
        counts = _counts(members)
        states[state] = {
            **counts,
            "positive_rate": _rate(
                counts["positive_count"], counts["resolved_count"], "no resolved labels"
            ),
            "interpretation": "OBSERVATIONAL_ONLY",
        }
    return {
        "status": "MEASURED",
        "states": states,
        "rejection_confirmation_created": False,
        "allowed_states": sorted(MANUAL_OB_ALLOWED),
    }


def _failure_classifications(observations: Sequence[EvaluationObservation]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in observations:
        outcome = _outcome_class(item)
        if outcome == "POSITIVE":
            continue
        if outcome == "UNRESOLVED":
            counts["unresolved_outcome"] += 1
            continue
        tokens = {value.upper() for value in (*item.blockers, *item.warnings)}
        data_status = str(item.context.get("data_status", "")).upper()
        if data_status == "STALE" or any("STALE" in value for value in tokens):
            counts["stale_evidence"] += 1
        elif any("ZONE_INVALID" in value for value in tokens):
            counts["zone_invalidation"] += 1
        elif any(
            value in tokens for value in ("STRUCTURE_CONTRADICTION", "HTF_DIRECTION_MISMATCH")
        ):
            counts["context_conflict"] += 1
        elif any("CONFIRMATION" in value for value in tokens):
            counts["insufficient_confirmation"] += 1
        elif item.score is None or data_status in {"INVALID", "INSUFFICIENT_DATA"}:
            counts["missing_data"] += 1
        elif item.score < 45:
            counts["weak_evidence"] += 1
        else:
            counts["unclassified_observation"] += 1
    return dict(sorted(counts.items()))


def _temporal_split(observations: Sequence[EvaluationObservation]) -> dict[str, Any]:
    ordered = sorted(observations, key=lambda item: (item.timestamp, item.observation_id))
    if len(ordered) < 10:
        return {
            "status": "INSUFFICIENT_SAMPLE",
            "chronological": True,
            "reason": "at least 10 observations are required before defining a holdout",
            "development": _dataset_metrics(ordered),
            "holdout": _dataset_metrics(()),
        }
    split_index = max(1, min(len(ordered) - 1, math.ceil(len(ordered) * 0.7)))
    cutoff = ordered[split_index].timestamp
    development = tuple(item for item in ordered if item.timestamp < cutoff)
    holdout = tuple(item for item in ordered if item.timestamp >= cutoff)
    return {
        "status": "AVAILABLE",
        "chronological": True,
        "cutoff": cutoff.isoformat(),
        "development": {
            **_dataset_metrics(development),
            "max_timestamp": development[-1].timestamp.isoformat() if development else None,
        },
        "holdout": {
            **_dataset_metrics(holdout),
            "min_timestamp": holdout[0].timestamp.isoformat() if holdout else None,
        },
        "thresholds_tuned_on_holdout": False,
    }


def _issue(index: int, code: str, detail: str) -> dict[str, Any]:
    return {"index": index, "code": code, "detail": detail}


def _coerce_observations(
    records: Iterable[EvaluationObservation | Mapping[str, Any]],
) -> tuple[list[EvaluationObservation], list[dict[str, Any]]]:
    parsed: list[tuple[int, EvaluationObservation]] = []
    issues: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        try:
            item = raw if isinstance(raw, EvaluationObservation) else observation_from_mapping(raw)
            parsed.append((index, item))
        except (EvaluationInputError, TypeError, ValueError, KeyError) as exc:
            code = (
                "SAFETY_VIOLATION"
                if "execution" in str(exc).lower() or "rejection" in str(exc).lower()
                else "MALFORMED_RECORD"
            )
            issues.append(_issue(index, code, str(exc)))
    by_id: dict[str, list[int]] = defaultdict(list)
    for index, item in parsed:
        by_id[item.observation_id].append(index)
    duplicate_indexes = {
        index for indexes in by_id.values() if len(indexes) > 1 for index in indexes
    }
    for observation_id, indexes in by_id.items():
        if len(indexes) > 1:
            issues.append(_issue(indexes[0], "DUPLICATE_OBSERVATION", observation_id))
    valid = [item for index, item in parsed if index not in duplicate_indexes]
    valid.sort(key=lambda item: (item.timestamp, item.observation_id))
    return valid, sorted(issues, key=lambda item: (item["index"], item["code"]))


def evaluate_records(
    records: Iterable[EvaluationObservation | Mapping[str, Any]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate records without mutating any source or runtime state."""

    observations, issues = _coerce_observations(records)
    generated = generated_at or datetime.now(UTC)
    if generated.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    counts = _counts(observations)
    baseline = _classifier_metrics(
        observations,
        lambda item: item.baseline_positive,
        unavailable_reason="canonical baseline prediction is not linked by stable provenance",
    )
    intelligence = _classifier_metrics(
        observations,
        lambda item: item.disposition == "ALERT" if item.disposition is not None else None,
        unavailable_reason="no resolved outcome labels or intelligence disposition",
    )
    return {
        "evaluation_version": EVALUATION_VERSION,
        "generated_at": generated.isoformat(),
        "execution_allowed": False,
        "data_coverage": {
            "sample_count": counts["observation_count"],
            "earliest": observations[0].timestamp.isoformat() if observations else None,
            "latest": observations[-1].timestamp.isoformat() if observations else None,
            "symbols": sorted({item.symbol for item in observations}),
            "timeframes": sorted({item.timeframe for item in observations}),
        },
        "source_versions": {
            "intelligence_versions": sorted({item.intelligence_version for item in observations}),
            "runtime_versions": sorted(
                {item.intelligence_runtime_version for item in observations}
            ),
            "evidence_versions": sorted(
                {item.evidence_version for item in observations if item.evidence_version}
            ),
        },
        "sample_counts": counts,
        "baseline_metrics": baseline,
        "intelligence_metrics": intelligence,
        "segmented_metrics": _segmented_metrics(observations),
        "component_metrics": _component_metrics(observations),
        "manual_ob_metrics": _manual_ob_metrics(observations),
        "failure_classifications": _failure_classifications(observations),
        "temporal_split": _temporal_split(observations),
        "invalid_records": issues,
        "limitations": [
            "This evaluator measures association and does not establish causation.",
            (
                "Unresolved outcomes are excluded from outcome and classifier rates, "
                "never converted to zero."
            ),
            "Score and confidence are not calibrated probabilities under the current contract.",
        ],
    }


def observations_from_persisted_rows(
    intelligence_rows: Iterable[Any],
    *,
    forward_signal_rows: Iterable[Any] = (),
    forward_trade_rows: Iterable[Any] = (),
) -> list[EvaluationObservation]:
    """Build observations using explicit Forward IDs only.

    A missing ``forward_signal_id`` remains unresolved.  No timestamp-based
    fallback is permitted, even when timestamps happen to match.
    """

    signals = {
        getattr(row, "id", None): row for row in forward_signal_rows if getattr(row, "id", None)
    }
    trades = {
        getattr(row, "signal_id", None): row
        for row in forward_trade_rows
        if getattr(row, "signal_id", None)
    }
    result: list[EvaluationObservation] = []
    for row in intelligence_rows:
        signal = signals.get(getattr(row, "forward_signal_id", None))
        trade = trades.get(getattr(row, "forward_signal_id", None))
        evidence = getattr(row, "evidence_json", None) or []
        components = getattr(row, "score_components_json", None) or []
        result.append(
            EvaluationObservation(
                observation_id=str(row.candidate_id),
                symbol=str(row.symbol),
                timeframe=str(row.timeframe),
                timestamp=row.detected_at,
                pair_zone_source={
                    "strategy": getattr(row, "strategy", None),
                    "strategy_version": getattr(row, "strategy_version", None),
                    "forward_session_id": getattr(row, "forward_session_id", None),
                    "forward_signal_id": getattr(row, "forward_signal_id", None),
                },
                intelligence_version=RUNTIME_VERSION,
                intelligence_runtime_version=RUNTIME_VERSION,
                evidence_version=getattr(row, "evidence_version", None),
                score=getattr(row, "score", None),
                confidence_band=getattr(row, "confidence_band", None),
                disposition=getattr(row, "alert_decision", None),
                state=getattr(row, "state", None),
                direction=getattr(row, "direction", None),
                evidence=tuple(evidence),
                score_components=tuple(components),
                blockers=tuple(getattr(row, "blockers_json", None) or ()),
                warnings=tuple(getattr(row, "warnings_json", None) or ()),
                context=getattr(row, "context_json", None) or {},
                baseline_positive=(getattr(signal, "decision", None) in {"BUY", "SELL"})
                if signal
                else None,
                outcome_status=getattr(trade, "state", None) if trade else None,
                outcome_realized_r=getattr(trade, "net_r", None) if trade else None,
                outcome_timestamp=getattr(trade, "terminal_timestamp", None) if trade else None,
                execution_allowed=getattr(row, "execution_allowed", False),
            )
        )
    return sorted(result, key=lambda item: (item.timestamp, item.observation_id))


def write_artifact(result: Mapping[str, Any], output: Path) -> None:
    """Write an explicitly requested research artifact, never a runtime table."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
