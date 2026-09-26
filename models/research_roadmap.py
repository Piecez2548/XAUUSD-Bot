"""Bounded read contracts for the Research Roadmap API V1."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROADMAP_API_CONTRACT_VERSION = "research_roadmap_api_v1"


class RoadmapRunV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=100)
    pipeline_key: str = Field(min_length=1, max_length=200)
    pipeline_contract_version: str = Field(min_length=1, max_length=64)
    lifecycle_state: str = Field(min_length=1, max_length=16)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    current_stage_key: str | None = Field(default=None, max_length=64)
    execution_allowed: bool = False


class RoadmapStageV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage_key: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    lifecycle_state: str = Field(min_length=1, max_length=16)
    processed: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=64)
    progress_mode: str = Field(pattern=r"^(DETERMINATE|INDETERMINATE)$")
    display_percentage: float | None = Field(default=None, ge=0, le=100)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=100)
    is_current_attempt: bool
    retry_count: int = Field(ge=0)
    execution_allowed: bool = False


class RoadmapDatasetSummaryV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates_inspected: int = Field(ge=0)
    rows_produced: int = Field(ge=0)
    trainable_rows: int = Field(ge=0)
    non_trainable_rows: int = Field(ge=0)
    unresolved_rows: int = Field(ge=0)
    excluded_rows: int = Field(ge=0)
    semantic_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_contract_version: str = Field(min_length=1, max_length=64)
    feature_contract_version: str = Field(min_length=1, max_length=64)
    label_contract_version: str = Field(min_length=1, max_length=64)


class RoadmapSourceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1, max_length=64)
    source_logical_id: str | None = Field(default=None, max_length=128)
    symbol_policy: str | None = Field(default=None, max_length=64)
    causal_cutoff_policy: str | None = Field(default=None, max_length=64)
    continuity_policy: str | None = Field(default=None, max_length=64)
    contract_versions: dict[str, str] = Field(default_factory=dict, max_length=8)


class RoadmapArtifactV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=100)
    artifact_type: str = Field(min_length=1, max_length=64)
    logical_identity: str = Field(min_length=1, max_length=200)
    validation_state: str = Field(min_length=1, max_length=16)
    publication_state: str = Field(min_length=1, max_length=16)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_dataset_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    producer_stage: str = Field(min_length=1, max_length=64)
    producer_attempt: int = Field(ge=1)
    created_at: datetime
    published_at: datetime | None = None


class RoadmapEventV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(min_length=1, max_length=64)
    timestamp: datetime
    stage_key: str | None = Field(default=None, max_length=64)
    attempt: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=8)


class RoadmapTimelineV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: str = ROADMAP_API_CONTRACT_VERSION
    run_id: str = Field(min_length=1, max_length=100)
    events: tuple[RoadmapEventV1, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class RoadmapRunHistoryV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=100)
    pipeline_key: str = Field(min_length=1, max_length=200)
    lifecycle_state: str = Field(min_length=1, max_length=16)
    created_at: datetime
    completed_at: datetime | None = None


class RoadmapResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: str = ROADMAP_API_CONTRACT_VERSION
    current_run: RoadmapRunV1 | None = None
    stages: tuple[RoadmapStageV1, ...] = Field(default_factory=tuple, max_length=32)
    dataset_summary: RoadmapDatasetSummaryV1 | None = None
    artifacts: tuple[RoadmapArtifactV1, ...] = Field(default_factory=tuple, max_length=16)
    timeline_preview: tuple[RoadmapEventV1, ...] = Field(default_factory=tuple, max_length=10)
    source: RoadmapSourceV1 | None = None
    recent_runs: tuple[RoadmapRunHistoryV1, ...] = Field(
        default_factory=tuple, max_length=200
    )
