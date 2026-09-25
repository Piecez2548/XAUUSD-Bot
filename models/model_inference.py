"""Immutable contract for deterministic, advisory-only inference V1."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.intelligence import AlertDecision, CandidateState, Direction


class ModelInferenceInputV1(BaseModel):
    """Minimal versioned references and fields from persisted intelligence evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_version: Literal["model_inference_input_v1"] = "model_inference_input_v1"
    source_intelligence_record_id: str = Field(min_length=1, max_length=36)
    source_candidate_id: str = Field(min_length=1, max_length=100)
    forward_session_id: str | None = Field(default=None, max_length=36)
    forward_signal_id: str | None = Field(default=None, max_length=36)
    pair_zone_event_id: str | None = Field(default=None, max_length=100)
    strategy_config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    strategy_version: str = Field(min_length=1, max_length=64)
    evidence_version: str = Field(min_length=1, max_length=64)
    source_detected_at: datetime
    source_state: CandidateState
    source_direction: Direction
    source_alert_decision: AlertDecision
    source_blocker_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_signal_reference(self) -> ModelInferenceInputV1:
        if self.forward_signal_id is not None and self.forward_session_id is None:
            raise ValueError("a forward signal reference requires its session")
        if self.source_detected_at.utcoffset() is None:
            raise ValueError("source evidence timestamp must be timezone-aware")
        return self


class AdvisoryClassification(StrEnum):
    SUPPORTIVE = "SUPPORTIVE"
    CAUTION = "CAUTION"
    OBSERVATION_ONLY = "OBSERVATION_ONLY"


class ModelInferenceOutputV1(BaseModel):
    """Non-executable advisory result with explicit provenance and version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_version: Literal["model_inference_output_v1"] = "model_inference_output_v1"
    inference_version: Literal["deterministic_advisory_v1"] = "deterministic_advisory_v1"
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evaluation_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    advisory_classification: AdvisoryClassification
    reason_codes: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = Field(
        min_length=1, max_length=8
    )
    execution_allowed: Literal[False] = False
