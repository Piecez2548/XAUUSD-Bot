"""Deterministic local inference V1; outputs are advisory and never executable."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.intelligence import AlertDecision, CandidateState
from models.model_inference import (
    AdvisoryClassification,
    ModelInferenceInputV1,
    ModelInferenceOutputV1,
)

INFERENCE_VERSION = "deterministic_advisory_v1"


class InferenceIdempotencyConflict(RuntimeError):
    """The same inference identity was presented with different source evidence."""


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_inference_input(
    source: Any,
    *,
    forward_session_id: str | None = None,
    forward_signal_id: str | None = None,
    strategy_config_hash: str | None = None,
) -> ModelInferenceInputV1:
    """Select only reproducibility fields from a persisted intelligence row."""

    source_session_id = getattr(source, "forward_session_id", None)
    selected_session_id = forward_session_id or source_session_id
    if source_session_id is not None and selected_session_id != source_session_id:
        raise ValueError("source candidate session does not match inference session")
    source_signal_id = getattr(source, "forward_signal_id", None)
    selected_signal_id = forward_signal_id or source_signal_id
    if source_signal_id is not None and selected_signal_id != source_signal_id:
        raise ValueError("source candidate signal does not match inference signal")

    return ModelInferenceInputV1(
        source_intelligence_record_id=str(source.id),
        source_candidate_id=str(source.candidate_id),
        forward_session_id=selected_session_id,
        forward_signal_id=selected_signal_id,
        pair_zone_event_id=getattr(source, "pair_zone_event_id", None),
        strategy_config_hash=strategy_config_hash,
        strategy_version=str(source.strategy_version),
        evidence_version=str(source.evidence_version),
        source_detected_at=source.detected_at,
        source_state=CandidateState(source.state),
        source_direction=source.direction,
        source_alert_decision=AlertDecision(source.alert_decision),
        source_blocker_count=len(source.blockers_json or ()),
    )


def evaluate_advisory(
    inference_input: ModelInferenceInputV1,
    *,
    evaluated_at: datetime | None = None,
) -> ModelInferenceOutputV1:
    """Map versioned advisory evidence to a deterministic non-actionable class."""

    serialized = inference_input.model_dump(mode="json")
    fingerprint = hashlib.sha256(_stable_json(serialized).encode("utf-8")).hexdigest()
    identity = f"{INFERENCE_VERSION}\0{inference_input.source_candidate_id}"
    evaluation_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    if (
        inference_input.source_blocker_count > 0
        or inference_input.source_alert_decision == AlertDecision.BLOCK
        or inference_input.source_state in {CandidateState.INVALIDATED, CandidateState.EXPIRED}
    ):
        classification = AdvisoryClassification.CAUTION
        reasons = ("SOURCE_EVIDENCE_BLOCKED",)
    elif inference_input.source_alert_decision == AlertDecision.ALERT:
        classification = AdvisoryClassification.SUPPORTIVE
        reasons = ("SOURCE_ALERT_EVIDENCE",)
    else:
        classification = AdvisoryClassification.OBSERVATION_ONLY
        reasons = ("SOURCE_NOT_ACTIONABLE",)

    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.utcoffset() is None:
        raise ValueError("evaluation timestamp must be timezone-aware")
    return ModelInferenceOutputV1(
        evaluated_at=timestamp.astimezone(UTC),
        evaluation_key=evaluation_key,
        input_fingerprint=fingerprint,
        advisory_classification=classification,
        reason_codes=reasons,
        execution_allowed=False,
    )


def persist_advisory_evaluation(
    database: Any,
    *,
    candidate_id: str,
    forward_session_id: str | None = None,
    forward_signal_id: str | None = None,
) -> Any:
    """Evaluate persisted evidence and insert one idempotent separate result."""

    from persistence.orm import (
        ForwardSignalRecord,
        ForwardValidationSessionRecord,
        ModelInferenceEvaluationRecord,
        StrategyIntelligenceRecord,
    )

    def _evaluate_in_session(session):
        source = session.scalar(
            select(StrategyIntelligenceRecord).where(
                StrategyIntelligenceRecord.candidate_id == candidate_id
            )
        )
        if source is None:
            raise ValueError("persisted intelligence evidence is unavailable")

        selected_session_id = forward_session_id or source.forward_session_id
        config_hash = None
        if selected_session_id is not None:
            forward_session = session.get(ForwardValidationSessionRecord, selected_session_id)
            if forward_session is None or source.forward_session_id != forward_session.id:
                raise ValueError("persisted forward session provenance is invalid")
            config_hash = forward_session.strategy_config_hash

        selected_signal_id = forward_signal_id or source.forward_signal_id
        if selected_signal_id is not None:
            signal = session.get(ForwardSignalRecord, selected_signal_id)
            if (
                signal is None
                or selected_session_id is None
                or signal.session_id != selected_session_id
                or source.forward_signal_id != signal.id
            ):
                raise ValueError("persisted forward signal provenance is invalid")

        inference_input = build_inference_input(
            source,
            forward_session_id=selected_session_id,
            forward_signal_id=selected_signal_id,
            strategy_config_hash=config_hash,
        )
        output = evaluate_advisory(inference_input)
        existing = session.scalar(
            select(ModelInferenceEvaluationRecord).where(
                ModelInferenceEvaluationRecord.evaluation_key == output.evaluation_key
            )
        )
        input_json = inference_input.model_dump(mode="json")
        if existing is not None:
            if existing.input_fingerprint != output.input_fingerprint:
                raise InferenceIdempotencyConflict(
                    "inference identity conflicts with persisted source evidence"
                )
            return existing

        row = ModelInferenceEvaluationRecord(
            evaluation_key=output.evaluation_key,
            inference_version=output.inference_version,
            evaluated_at=output.evaluated_at,
            source_intelligence_record_id=source.id,
            source_candidate_id=source.candidate_id,
            forward_session_id=selected_session_id,
            forward_signal_id=selected_signal_id,
            pair_zone_event_id=source.pair_zone_event_id,
            strategy_config_hash=config_hash,
            input_fingerprint=output.input_fingerprint,
            input_json=input_json,
            advisory_classification=output.advisory_classification.value,
            reason_codes_json=list(output.reason_codes),
            execution_allowed=False,
        )
        session.add(row)
        session.flush()
        return row

    try:
        with database.session() as session:
            return _evaluate_in_session(session)
    except IntegrityError as race_error:
        # Concurrent duplicate evaluations converge on the same durable row.
        with database.session() as session:
            existing = session.scalar(
                select(ModelInferenceEvaluationRecord).where(
                    ModelInferenceEvaluationRecord.evaluation_key
                    == hashlib.sha256(
                        f"{INFERENCE_VERSION}\0{candidate_id}".encode()
                    ).hexdigest()
                )
            )
            if existing is None:
                raise
            current_source = session.scalar(
                select(StrategyIntelligenceRecord).where(
                    StrategyIntelligenceRecord.candidate_id == candidate_id
                )
            )
            if current_source is None:
                raise
            current_input = build_inference_input(
                current_source,
                forward_session_id=existing.forward_session_id,
                forward_signal_id=existing.forward_signal_id,
                strategy_config_hash=existing.strategy_config_hash,
            )
            fingerprint = hashlib.sha256(
                _stable_json(current_input.model_dump(mode="json")).encode("utf-8")
            ).hexdigest()
            if existing.input_fingerprint != fingerprint:
                raise InferenceIdempotencyConflict(
                    "concurrent inference identity conflicts with source evidence"
                ) from race_error
            return existing
