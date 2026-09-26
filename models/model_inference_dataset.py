"""Frozen contracts for the read-only Model Inference V2 dataset foundation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DATASET_CONTRACT_VERSION = "model_inference_dataset_v1"
FEATURE_CONTRACT_VERSION = "model_training_features_v1"
ARTIFACT_CONTRACT_VERSION = "model_inference_artifact_v1"

DatasetState = Literal[
    "PRE_SIGNAL_OBSERVATION",
    "SIGNAL_ELIGIBLE",
    "OUTCOME_PENDING",
    "OUTCOME_COMPLETED",
    "UNRESOLVED",
]
TrainingEligibility = Literal["TRAINABLE", "NON_TRAINABLE", "UNRESOLVED"]


class DatasetRowV1(BaseModel):
    """One immutable, auditable dataset row.

    The nested provenance fields are explicit contracts, not copies of the
    source evidence JSON.  This prevents arbitrary broker/account data from
    becoming a training feature by accident.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    dataset_contract_version: Literal["model_inference_dataset_v1"] = (
        DATASET_CONTRACT_VERSION
    )
    feature_contract_version: Literal["model_training_features_v1"] = (
        FEATURE_CONTRACT_VERSION
    )
    candidate_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(min_length=1, max_length=64)
    candidate_timestamp: datetime
    causal_cutoff_timestamp: datetime
    state: DatasetState
    training_eligibility: TrainingEligibility
    reason_codes: tuple[str, ...] = Field(max_length=16)
    classification: str | None = None
    outcome_label: str
    features: dict[str, Any] = Field(default_factory=dict)
    source_provenance: dict[str, Any] = Field(default_factory=dict)
    source_timestamps: dict[str, datetime | None] = Field(default_factory=dict)
    row_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class DatasetAuditV1(BaseModel):
    """Descriptive audit counts; it intentionally contains no accuracy metric."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_count: int = Field(ge=0)
    inspected_candidate_count: int = Field(ge=0)
    builder_exclusion_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    trainable_count: int = Field(ge=0)
    non_trainable_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    state_counts: dict[str, int]
    label_counts: dict[str, int]
    reason_counts: dict[str, int]
    classification_counts: dict[str, int]
    symbol_counts: dict[str, int]
    contract_version_counts: dict[str, int]
    missing_feature_counts: dict[str, int]
    causal_violation_count: int = Field(ge=0)
    duplicate_identity_count: int = Field(ge=0)


class DatasetManifestV1(BaseModel):
    """Versioned sidecar manifest for an artifact and its deterministic hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_contract_version: Literal["model_inference_artifact_v1"] = (
        ARTIFACT_CONTRACT_VERSION
    )
    dataset_contract_version: Literal["model_inference_dataset_v1"] = (
        DATASET_CONTRACT_VERSION
    )
    feature_contract_version: Literal["model_training_features_v1"] = (
        FEATURE_CONTRACT_VERSION
    )
    artifact_format: Literal["jsonl", "parquet"]
    artifact_name: str
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    audit: DatasetAuditV1
    causal_cutoff_rule: str
    bounded_page_size: int = Field(gt=0, le=500)
    session_aware_v2_accepted: bool
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
