"""Read-only, bounded evaluation of advisory classifications against Forward outcomes.

This module derives its view from the existing immutable inference evidence and
Forward virtual-trade rows. It deliberately stores no duplicate outcome data
and is not called from the Live decision or execution path.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select

from models.model_inference import AdvisoryClassification
from persistence.orm import (
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    ModelInferenceEvaluationRecord,
    StrategyIntelligenceRecord,
)
from services.model_inference import INFERENCE_VERSION

COMPLETED_FORWARD_STATES = frozenset({"TP", "SL", "AMBIGUOUS", "EXPIRED"})
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class OutcomeCursor:
    """Stable keyset position; id breaks evaluated_at ties."""

    evaluated_at: datetime
    row_id: str


@dataclass(frozen=True, slots=True)
class InferenceOutcomeEvaluation:
    """Minimal derived row; the Forward trade remains the outcome authority."""

    evaluation_id: str
    evaluation_key: str
    candidate_id: str
    classification: str | None
    disposition: str
    forward_session_id: str | None
    forward_signal_id: str | None
    forward_trade_id: str | None
    outcome_state: str | None
    gross_r: float | None
    net_r: float | None


@dataclass(frozen=True, slots=True)
class OutcomeEvaluationPage:
    items: tuple[InferenceOutcomeEvaluation, ...]
    next_cursor: OutcomeCursor | None


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _derive_row(
    evaluation: ModelInferenceEvaluationRecord,
    source: StrategyIntelligenceRecord | None,
    forward_session: ForwardValidationSessionRecord | None,
    signal: ForwardSignalRecord | None,
    trade: ForwardTradeRecord | None,
) -> InferenceOutcomeEvaluation:
    raw_classification = evaluation.advisory_classification
    try:
        classification = AdvisoryClassification(raw_classification).value
    except (TypeError, ValueError):
        classification = None

    disposition = "UNRESOLVED"
    outcome_state = None
    gross_r = net_r = None
    trade_id = trade.id if trade is not None else None

    provenance_valid = (
        source is not None
        and classification is not None
        and source.id == evaluation.source_intelligence_record_id
        and source.candidate_id == evaluation.source_candidate_id
        and source.forward_session_id == evaluation.forward_session_id
        and source.forward_signal_id == evaluation.forward_signal_id
        and source.pair_zone_event_id == evaluation.pair_zone_event_id
        and evaluation.execution_allowed is False
        and source.execution_allowed is False
    )
    if provenance_valid and evaluation.forward_session_id is not None:
        provenance_valid = (
            forward_session is not None
            and forward_session.id == evaluation.forward_session_id
            and source is not None
            and source.forward_session_id == forward_session.id
            and evaluation.strategy_config_hash == forward_session.strategy_config_hash
            and forward_session.execution_allowed is False
            and source.symbol == forward_session.symbol
            and source.strategy == forward_session.strategy_id
            and source.strategy_version == forward_session.strategy_version
        )
    elif provenance_valid and forward_session is not None:
        provenance_valid = False

    if provenance_valid and evaluation.forward_signal_id is None:
        # No canonical signal means there is no eligible Forward trade to score.
        if source is not None and source.forward_trade_id is None and trade is None:
            disposition = "NOT_ELIGIBLE"
    elif provenance_valid and evaluation.forward_signal_id is not None:
        provenance_valid = (
            forward_session is not None
            and signal is not None
            and signal.id == evaluation.forward_signal_id
            and signal.session_id == evaluation.forward_session_id
            and signal.zone_id == evaluation.pair_zone_event_id
            and signal.strategy_hash == evaluation.strategy_config_hash
            and signal.execution_allowed is False
            and trade is not None
            and trade.signal_id == signal.id
            and trade.session_id == evaluation.forward_session_id
            and trade.timestamp == signal.timestamp
            and source is not None
            and source.forward_trade_id == trade.id
            and trade.execution_allowed is False
            and signal.decision in {"BUY", "SELL"}
            and trade.side == signal.decision
            and source.direction == signal.decision
        )
        if provenance_valid and trade is not None:
            outcome_state = (
                trade.state
                if trade.state == "OPEN" or trade.state in COMPLETED_FORWARD_STATES
                else None
            )
            if trade.state == "OPEN":
                if (
                    trade.terminal_timestamp is None
                    and trade.evaluated_at is None
                    and trade.gross_r is None
                    and trade.net_r is None
                ):
                    disposition = "PENDING"
            elif trade.state in COMPLETED_FORWARD_STATES:
                gross_r = _finite_or_none(trade.gross_r)
                net_r = _finite_or_none(trade.net_r)
                completion_valid = (
                    trade.terminal_timestamp is not None
                    and trade.evaluated_at is not None
                    and trade.terminal_timestamp > trade.timestamp
                    and source is not None
                    and trade.terminal_timestamp > source.detected_at
                    and (
                        trade.state == "AMBIGUOUS"
                        and trade.gross_r is None
                        and trade.net_r is None
                        or trade.state != "AMBIGUOUS"
                        and gross_r is not None
                        and net_r is not None
                    )
                )
                if completion_valid:
                    disposition = "COMPLETED"
                else:
                    gross_r = net_r = None
            # Unknown or internally inconsistent trade states remain unresolved.

    return InferenceOutcomeEvaluation(
        evaluation_id=evaluation.id,
        evaluation_key=evaluation.evaluation_key,
        candidate_id=evaluation.source_candidate_id,
        classification=classification,
        disposition=disposition,
        forward_session_id=evaluation.forward_session_id,
        forward_signal_id=evaluation.forward_signal_id,
        forward_trade_id=trade_id,
        outcome_state=outcome_state,
        gross_r=gross_r,
        net_r=net_r,
    )


def _unresolved_row(evaluation: ModelInferenceEvaluationRecord) -> InferenceOutcomeEvaluation:
    """Keep an individually unusable row visible without trusting its evidence."""

    try:
        classification = AdvisoryClassification(evaluation.advisory_classification).value
    except (TypeError, ValueError):
        classification = None
    return InferenceOutcomeEvaluation(
        evaluation_id=evaluation.id,
        evaluation_key=evaluation.evaluation_key,
        candidate_id=evaluation.source_candidate_id,
        classification=classification,
        disposition="UNRESOLVED",
        forward_session_id=evaluation.forward_session_id,
        forward_signal_id=evaluation.forward_signal_id,
        forward_trade_id=None,
        outcome_state=None,
        gross_r=None,
        net_r=None,
    )


def list_inference_outcome_evaluations(
    database: Any,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    after: OutcomeCursor | None = None,
) -> OutcomeEvaluationPage:
    """Read one bounded page in deterministic evaluation-time/id order."""

    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if after is not None and (after.evaluated_at.utcoffset() is None or not after.row_id):
        raise ValueError("cursor must contain an aware timestamp and row id")

    statement = (
        select(
            ModelInferenceEvaluationRecord,
            StrategyIntelligenceRecord,
            ForwardValidationSessionRecord,
            ForwardSignalRecord,
            ForwardTradeRecord,
        )
        .outerjoin(
            StrategyIntelligenceRecord,
            StrategyIntelligenceRecord.id
            == ModelInferenceEvaluationRecord.source_intelligence_record_id,
        )
        .outerjoin(
            ForwardValidationSessionRecord,
            ForwardValidationSessionRecord.id == ModelInferenceEvaluationRecord.forward_session_id,
        )
        .outerjoin(
            ForwardSignalRecord,
            ForwardSignalRecord.id == ModelInferenceEvaluationRecord.forward_signal_id,
        )
        .outerjoin(
            ForwardTradeRecord,
            ForwardTradeRecord.signal_id == ModelInferenceEvaluationRecord.forward_signal_id,
        )
        .order_by(
            ModelInferenceEvaluationRecord.evaluated_at,
            ModelInferenceEvaluationRecord.id,
        )
        .where(ModelInferenceEvaluationRecord.inference_version == INFERENCE_VERSION)
    )
    if after is not None:
        statement = statement.where(
            or_(
                ModelInferenceEvaluationRecord.evaluated_at > after.evaluated_at,
                and_(
                    ModelInferenceEvaluationRecord.evaluated_at == after.evaluated_at,
                    ModelInferenceEvaluationRecord.id > after.row_id,
                ),
            )
        )
    statement = statement.limit(page_size + 1)

    with database.session() as session:
        fetched = session.execute(statement).all()

    has_more = len(fetched) > page_size
    selected = fetched[:page_size]
    items_list: list[InferenceOutcomeEvaluation] = []
    for row in selected:
        try:
            items_list.append(_derive_row(*row))
        except Exception:
            # A malformed individual record must not abort other read-only
            # results. Do not log or retain exception details/evidence.
            items_list.append(_unresolved_row(row[0]))
    items = tuple(items_list)
    next_cursor = None
    if has_more and selected:
        last = selected[-1][0]
        next_cursor = OutcomeCursor(last.evaluated_at, last.id)
    return OutcomeEvaluationPage(items=items, next_cursor=next_cursor)


def summarize_inference_outcomes(
    database: Any, *, page_size: int = DEFAULT_PAGE_SIZE
) -> dict[str, object]:
    """Aggregate descriptive counts/R values using bounded pages, never writes."""

    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    classes = [classification.value for classification in AdvisoryClassification]
    buckets: dict[str, dict[str, object]] = {
        classification: {
            "evaluation_count": 0,
            "completed_outcome_count": 0,
            "pending_count": 0,
            "unresolved_count": 0,
            "not_eligible_count": 0,
            "outcome_counts": Counter(),
            "win_count": 0,
            "loss_count": 0,
            "flat_count": 0,
            "net_r_sum": 0.0,
            "net_r_sample_count": 0,
            "gross_r_sum": 0.0,
            "gross_r_sample_count": 0,
        }
        for classification in classes
    }
    total_dispositions: Counter[str] = Counter()
    unclassified_count = 0
    cursor: OutcomeCursor | None = None

    while True:
        page = list_inference_outcome_evaluations(database, page_size=page_size, after=cursor)
        for item in page.items:
            total_dispositions[item.disposition] += 1
            if item.classification not in buckets:
                unclassified_count += 1
                continue
            bucket = buckets[item.classification]
            bucket["evaluation_count"] = int(bucket["evaluation_count"]) + 1
            count_key = {
                "COMPLETED": "completed_outcome_count",
                "PENDING": "pending_count",
                "UNRESOLVED": "unresolved_count",
                "NOT_ELIGIBLE": "not_eligible_count",
            }[item.disposition]
            bucket[count_key] = int(bucket[count_key]) + 1
            if item.disposition != "COMPLETED":
                continue
            outcome_counts = bucket["outcome_counts"]
            assert isinstance(outcome_counts, Counter)
            if item.outcome_state is not None:
                outcome_counts[item.outcome_state] += 1
            if item.net_r is not None:
                bucket["net_r_sum"] = float(bucket["net_r_sum"]) + item.net_r
                bucket["net_r_sample_count"] = int(bucket["net_r_sample_count"]) + 1
                sign_key = (
                    "win_count"
                    if item.net_r > 0
                    else "loss_count"
                    if item.net_r < 0
                    else "flat_count"
                )
                bucket[sign_key] = int(bucket[sign_key]) + 1
            if item.gross_r is not None:
                bucket["gross_r_sum"] = float(bucket["gross_r_sum"]) + item.gross_r
                bucket["gross_r_sample_count"] = int(bucket["gross_r_sample_count"]) + 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    by_classification: dict[str, object] = {}
    for classification, source in buckets.items():
        net_r_sum = float(source.pop("net_r_sum"))
        net_r_sample_count = int(source["net_r_sample_count"])
        gross_r_sum = float(source.pop("gross_r_sum"))
        gross_r_sample_count = int(source["gross_r_sample_count"])
        counts = source["outcome_counts"]
        assert isinstance(counts, Counter)
        by_classification[classification] = {
            **source,
            "outcome_counts": dict(sorted(counts.items())),
            "average_net_r": net_r_sum / net_r_sample_count if net_r_sample_count else None,
            "average_gross_r": gross_r_sum / gross_r_sample_count if gross_r_sample_count else None,
        }

    return {
        "evaluation_count": sum(total_dispositions.values()),
        "completed_outcome_count": total_dispositions["COMPLETED"],
        "pending_count": total_dispositions["PENDING"],
        "unresolved_count": total_dispositions["UNRESOLVED"],
        "not_eligible_count": total_dispositions["NOT_ELIGIBLE"],
        "unclassified_count": unclassified_count,
        "by_classification": by_classification,
    }
