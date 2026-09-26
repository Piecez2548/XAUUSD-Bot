"""Bounded V1B contracts for the read-only dataset research pipeline."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.research_pipeline import EmbeddedReferenceV1, QualityGateEvidenceV1

DATASET_PIPELINE_VERSION = "model_inference_dataset_pipeline_v1"
DATA_SOURCE_STAGE = "DATA_SOURCE"
DATASET_BUILD_STAGE = "DATASET_BUILD"
DATASET_AUDIT_STAGE = "DATASET_AUDIT"
ARTIFACT_VERIFY_STAGE = "ARTIFACT_VERIFY"
DATASET_PIPELINE_STAGES = (
    DATA_SOURCE_STAGE,
    DATASET_BUILD_STAGE,
    DATASET_AUDIT_STAGE,
    ARTIFACT_VERIFY_STAGE,
)
DATASET_PIPELINE_STAGE_VERSION = "v1"
DATASET_JSONL_ARTIFACT = "DATASET_JSONL"
DATASET_MANIFEST_ARTIFACT = "DATASET_MANIFEST"
DATASET_ARTIFACT_KINDS = (DATASET_JSONL_ARTIFACT, DATASET_MANIFEST_ARTIFACT)
_SAFE_LOGICAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHA256 = r"^[0-9a-f]{64}$"


class DatasetPipelineContractError(ValueError):
    """Raised when a bounded V1B contract cannot represent the result."""


class DatasetSourceDescriptorV1(BaseModel):
    """The validated logical source contract, without source-table contents."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    pipeline_version: Literal["model_inference_dataset_pipeline_v1"] = (
        DATASET_PIPELINE_VERSION
    )
    dataset_contract_version: Literal["model_inference_dataset_v1"]
    feature_contract_version: Literal["model_training_features_v1"]
    label_contract_version: str = Field(min_length=1, max_length=64)
    builder_semantics_version: str = Field(min_length=1, max_length=64)
    source_logical_id: str = Field(min_length=1, max_length=128)
    symbol_policy: Literal["EXACT_SYMBOL"]
    causal_cutoff_policy: Literal["CANDIDATE_DETECTED_AT_UTC"]
    continuity_policy: Literal["SESSION_AWARE_V2_ACCEPTED"]

    @model_validator(mode="after")
    def validate_logical_values(self) -> DatasetSourceDescriptorV1:
        for value in (
            self.label_contract_version,
            self.builder_semantics_version,
            self.source_logical_id,
        ):
            if not _SAFE_LOGICAL.fullmatch(value):
                raise DatasetPipelineContractError("source metadata must be logical identifiers")
        return self


class DatasetBuildSummaryV1(BaseModel):
    """Reconciled, bounded counters from one completed builder attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    candidates_inspected: int = Field(ge=0)
    rows_produced: int = Field(ge=0)
    trainable_rows: int = Field(ge=0)
    non_trainable_rows: int = Field(ge=0)
    unresolved_rows: int = Field(ge=0)
    excluded_rows: int = Field(ge=0)
    artifact_logical_identities: tuple[str, ...] = Field(min_length=2, max_length=2)
    dataset_semantic_hash: str = Field(pattern=_SHA256)
    dataset_contract_version: Literal["model_inference_dataset_v1"]
    feature_contract_version: Literal["model_training_features_v1"]
    builder_semantics_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def reconcile_counts(self) -> DatasetBuildSummaryV1:
        if (
            self.trainable_rows + self.non_trainable_rows + self.unresolved_rows
            != self.rows_produced
        ):
            raise DatasetPipelineContractError("dataset row counts do not reconcile")
        if self.candidates_inspected != self.rows_produced + self.excluded_rows:
            raise DatasetPipelineContractError("candidate counts do not reconcile")
        if any(not _SAFE_LOGICAL.fullmatch(item) for item in self.artifact_logical_identities):
            raise DatasetPipelineContractError("artifact identities must be logical identifiers")
        if not _SAFE_LOGICAL.fullmatch(self.builder_semantics_version):
            raise DatasetPipelineContractError("builder semantics must be a logical identifier")
        return self


class DatasetAuditReportV1(BaseModel):
    """Contract-level audit result; no profitability or approval semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    artifact_readable: bool
    manifest_readable: bool
    artifact_sha256_matches: bool
    manifest_sha256_matches: bool
    semantic_hash_matches: bool
    row_count_matches: bool
    row_fingerprints_valid: bool
    canonical_ordering_valid: bool
    contract_versions_valid: bool
    duplicate_candidate_identities: int = Field(ge=0)
    duplicate_row_fingerprints: int = Field(ge=0)
    summary_counts_match: bool
    training_eligibility_consistent: bool
    leakage_checks_passed: bool
    exact_symbol_policy_satisfied: bool
    continuity_policy_satisfied: bool
    summary: DatasetBuildSummaryV1
    failure_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @model_validator(mode="after")
    def validate_result(self) -> DatasetAuditReportV1:
        checks = (
            self.artifact_readable,
            self.manifest_readable,
            self.artifact_sha256_matches,
            self.manifest_sha256_matches,
            self.semantic_hash_matches,
            self.row_count_matches,
            self.row_fingerprints_valid,
            self.canonical_ordering_valid,
            self.contract_versions_valid,
            self.duplicate_candidate_identities == 0,
            self.duplicate_row_fingerprints == 0,
            self.summary_counts_match,
            self.training_eligibility_consistent,
            self.leakage_checks_passed,
            self.exact_symbol_policy_satisfied,
            self.continuity_policy_satisfied,
        )
        if all(checks) and self.failure_codes:
            raise DatasetPipelineContractError("a passing audit cannot contain failure codes")
        return self

    @property
    def passed(self) -> bool:
        return not self.failure_codes and all(
            (
                self.artifact_readable,
                self.manifest_readable,
                self.artifact_sha256_matches,
                self.manifest_sha256_matches,
                self.semantic_hash_matches,
                self.row_count_matches,
                self.row_fingerprints_valid,
                self.canonical_ordering_valid,
                self.contract_versions_valid,
                self.duplicate_candidate_identities == 0,
                self.duplicate_row_fingerprints == 0,
                self.summary_counts_match,
                self.training_eligibility_consistent,
                self.leakage_checks_passed,
                self.exact_symbol_policy_satisfied,
                self.continuity_policy_satisfied,
            )
        )


class DatasetPipelineStageViewV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    stage_run_id: str = Field(min_length=1, max_length=100)
    stage_key: Literal[
        "DATA_SOURCE", "DATASET_BUILD", "DATASET_AUDIT", "ARTIFACT_VERIFY"
    ]
    attempt_number: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=16)
    processed: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=64)
    started_at: str | None = None
    completed_at: str | None = None
    reason_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    gate_evidence: tuple[QualityGateEvidenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    lineage_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )

    @property
    def progress_mode(self) -> Literal["KNOWN", "INDETERMINATE"]:
        return "KNOWN" if self.total is not None else "INDETERMINATE"


class DatasetPipelineArtifactViewV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    artifact_id: str = Field(min_length=1, max_length=100)
    artifact_kind: Literal["DATASET_JSONL", "DATASET_MANIFEST"]
    logical_locator: str = Field(min_length=1, max_length=200)
    content_sha256: str = Field(pattern=_SHA256)
    validation_status: Literal["UNVALIDATED", "VALID", "INVALID"]
    publication_status: Literal["UNPUBLISHED", "PUBLISHED", "REJECTED"]
    producer_stage_run_id: str = Field(min_length=1, max_length=100)
    producer_attempt_number: int = Field(ge=1)
    lineage_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )


class DatasetPipelineViewV1(BaseModel):
    """Bounded read model for a future Roadmap UI."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    run_id: str = Field(min_length=1, max_length=100)
    pipeline_version: Literal["model_inference_dataset_pipeline_v1"]
    status: str = Field(min_length=1, max_length=16)
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    stages: tuple[DatasetPipelineStageViewV1, ...] = Field(max_length=32)
    artifacts: tuple[DatasetPipelineArtifactViewV1, ...] = Field(max_length=16)
    summary: DatasetBuildSummaryV1 | None = None
