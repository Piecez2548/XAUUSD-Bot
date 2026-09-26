"""Versioned contracts for the read-only research pipeline foundation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PIPELINE_RUN_CONTRACT_VERSION = "research_pipeline_run_v1"
STAGE_RUN_CONTRACT_VERSION = "research_stage_run_v1"
ARTIFACT_CONTRACT_VERSION = "research_artifact_v1"
EVIDENCE_CONTRACT_VERSION = "research_evidence_v1"

ResearchStatus = Literal[
    "WAITING",
    "READY",
    "RUNNING",
    "PASS",
    "BLOCKED",
    "FAILED",
    "SKIPPED",
    "CANCELLED",
]
ArtifactPublicationStatus = Literal["UNPUBLISHED", "PUBLISHED", "REJECTED"]
ArtifactValidationStatus = Literal["UNVALIDATED", "VALID", "INVALID"]
EvidenceStatus = Literal["PASS", "BLOCKED", "FAILED", "NOT_EVALUATED"]
Scalar = str | int | float | bool | None
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_LOGICAL_LOCATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|api_key|apikey|token|access_token|"
    r"refresh_token|authorization|bearer|private_key|credential|credentials|"
    r"dsn|connection_string|connection_url)(?:_|$)"
)
_UNSAFE_METADATA_VALUE = re.compile(
    r"(?:^[A-Za-z]:[\\/]|^\\\\|^//|^/|^~[\\/]|^file://|"
    r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^@\s]+@|"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]+|"
    r"-----BEGIN[^\r\n]*PRIVATE KEY-----|"
    r"\b(?:password|passwd|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"authorization|bearer|private[ _-]?key|credential)\s*[:=])",
    re.IGNORECASE,
)
_SECRET_PREFIX_VALUE = re.compile(
    r"(?:^|\b)(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
_SENSITIVE_KEY_PREFIXES = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "bearer",
    "privatekey",
    "credential",
    "dsn",
    "connectionstring",
    "connectionurl",
)


def _validate_metadata_text(value: str) -> str:
    if _UNSAFE_METADATA_VALUE.search(value) or _SECRET_PREFIX_VALUE.search(value):
        raise ValueError("unsafe metadata value")
    return value


def _validate_metadata_map(value: dict[str, Scalar]) -> dict[str, Scalar]:
    for key, item in value.items():
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        compact_key = normalized_key.replace("_", "")
        if (
            _UNSAFE_METADATA_VALUE.search(key)
            or _SECRET_PREFIX_VALUE.search(key)
            or _SENSITIVE_KEY.search(normalized_key)
            or any(
            compact_key == prefix or compact_key.startswith(prefix)
            for prefix in _SENSITIVE_KEY_PREFIXES
            )
        ):
            raise ValueError("unsafe metadata key")
        if isinstance(item, str):
            _validate_metadata_text(item)
    return value


def _validate_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    for item in value:
        _validate_metadata_text(item)
        if not _SAFE_LOGICAL_LOCATOR.fullmatch(item):
            raise ValueError("reason code must be a logical identifier")
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC) if value is not None else None


class EmbeddedReferenceV1(BaseModel):
    """Bounded logical reference; never a private path or credential."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    reference_type: str = Field(min_length=1, max_length=64)
    reference_id: str = Field(min_length=1, max_length=128)
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    contract_version: str | None = Field(default=None, max_length=64)

    @field_validator("reference_id")
    @classmethod
    def logical_id_only(cls, value: str) -> str:
        _validate_metadata_text(value)
        if "\\" in value or value.startswith(("/", "~")) or ".." in value:
            raise ValueError("reference_id must be a safe logical identifier")
        return value

    _validate_reference_type = field_validator("reference_type")(_validate_metadata_text)
    _validate_contract_version = field_validator("contract_version")(_validate_metadata_text)


class QualityGateEvidenceV1(BaseModel):
    """Evidence only; this structure has no trading or approval authority."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    evidence_contract_version: Literal["research_evidence_v1"] = EVIDENCE_CONTRACT_VERSION
    gate_id: str = Field(min_length=1, max_length=64)
    status: EvidenceStatus
    measured: dict[str, Scalar] = Field(default_factory=dict, max_length=32)
    evidence_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=16
    )
    reason_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    _validate_gate_id = field_validator("gate_id")(_validate_metadata_text)
    _validate_measured = field_validator("measured")(_validate_metadata_map)
    _validate_reason_codes = field_validator("reason_codes")(_validate_reason_codes)


class ResearchPipelineRun(BaseModel):
    """One immutable research experiment manifest and its lifecycle projection."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )

    contract_version: Literal["research_pipeline_run_v1"] = PIPELINE_RUN_CONTRACT_VERSION
    run_id: str = Field(min_length=1, max_length=100)
    pipeline_version: str = Field(min_length=1, max_length=64)
    experiment_key: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    requested_inputs: dict[str, Scalar] = Field(default_factory=dict, max_length=32)
    source_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    configuration_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    code_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    status: ResearchStatus = "WAITING"
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    terminal_reason: str | None = Field(default=None, max_length=200)
    execution_allowed: Literal[False] = False

    _validate_created_at = field_validator("created_at", "started_at", "completed_at")(_aware)
    _validate_requested_inputs = field_validator("requested_inputs")(_validate_metadata_map)
    _validate_pipeline_metadata = field_validator(
        "run_id",
        "pipeline_version",
        "experiment_key",
        "idempotency_key",
        "terminal_reason",
    )(_validate_metadata_text)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ResearchPipelineRun:
        if self.completed_at is not None and self.status not in {
            "PASS",
            "FAILED",
            "CANCELLED",
            "SKIPPED",
        }:
            raise ValueError("completed_at requires a terminal pipeline status")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot precede created_at")
        if (
            self.completed_at is not None
            and self.started_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at cannot precede started_at")
        return self


class ResearchStageRun(BaseModel):
    """One attempt of one pipeline stage; retries are separate records."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )

    contract_version: Literal["research_stage_run_v1"] = STAGE_RUN_CONTRACT_VERSION
    stage_run_id: str = Field(min_length=1, max_length=100)
    pipeline_run_id: str = Field(min_length=1, max_length=100)
    stage_key: str = Field(min_length=1, max_length=64)
    stage_version: str = Field(min_length=1, max_length=64)
    attempt_number: int = Field(ge=1)
    status: ResearchStatus = "READY"
    progress_processed: int = Field(default=0, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    progress_unit: str | None = Field(default=None, max_length=64)
    blocked_reason_code: str | None = Field(default=None, max_length=100)
    failure_reason_code: str | None = Field(default=None, max_length=100)
    terminal_reason: str | None = Field(default=None, max_length=200)
    input_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    evidence_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    gate_evidence: tuple[QualityGateEvidenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    lineage_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    execution_allowed: Literal[False] = False

    _validate_timestamps = field_validator("created_at", "started_at", "completed_at")(_aware)
    _validate_stage_metadata = field_validator(
        "stage_run_id",
        "pipeline_run_id",
        "stage_key",
        "stage_version",
        "progress_unit",
        "blocked_reason_code",
        "failure_reason_code",
        "terminal_reason",
    )(_validate_metadata_text)

    @model_validator(mode="after")
    def validate_progress_and_lifecycle(self) -> ResearchStageRun:
        if self.progress_total is not None and self.progress_processed > self.progress_total:
            raise ValueError("progress_processed cannot exceed progress_total")
        if self.progress_total is not None and not self.progress_unit:
            raise ValueError("progress_unit is required when progress_total is known")
        if self.completed_at is not None and self.status not in {
            "PASS",
            "FAILED",
            "BLOCKED",
            "CANCELLED",
            "SKIPPED",
        }:
            raise ValueError("completed_at requires a terminal stage status")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot precede created_at")
        if (
            self.completed_at is not None
            and self.started_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at cannot precede started_at")
        return self

    @property
    def progress_state(self) -> Literal["KNOWN", "INDETERMINATE"]:
        return "KNOWN" if self.progress_total is not None else "INDETERMINATE"


class ResearchArtifact(BaseModel):
    """Metadata-only artifact identity; bytes remain outside the application DB."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )

    contract_version: Literal["research_artifact_v1"] = ARTIFACT_CONTRACT_VERSION
    artifact_id: str = Field(min_length=1, max_length=100)
    artifact_kind: str = Field(min_length=1, max_length=64)
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    manifest_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_format: str = Field(min_length=1, max_length=32)
    size_bytes: int = Field(ge=0)
    logical_locator: str = Field(min_length=1, max_length=200)
    pipeline_run_id: str = Field(min_length=1, max_length=100)
    producer_stage_run_id: str = Field(min_length=1, max_length=100)
    publication_status: ArtifactPublicationStatus = "UNPUBLISHED"
    validation_status: ArtifactValidationStatus = "UNVALIDATED"
    gate_evidence: tuple[QualityGateEvidenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    lineage_references: tuple[EmbeddedReferenceV1, ...] = Field(
        default_factory=tuple, max_length=32
    )
    created_at: datetime
    published_at: datetime | None = None
    execution_allowed: Literal[False] = False

    _validate_timestamps = field_validator("created_at", "published_at")(_aware)
    _validate_artifact_metadata = field_validator(
        "artifact_id",
        "artifact_kind",
        "artifact_format",
        "pipeline_run_id",
        "producer_stage_run_id",
    )(_validate_metadata_text)

    @field_validator("logical_locator")
    @classmethod
    def safe_locator(cls, value: str) -> str:
        if not _SAFE_LOGICAL_LOCATOR.fullmatch(value):
            raise ValueError("logical_locator must be a relative logical identifier")
        return value

    @model_validator(mode="after")
    def validate_publication(self) -> ResearchArtifact:
        if self.publication_status == "PUBLISHED" and self.validation_status != "VALID":
            raise ValueError("published artifacts must be valid")
        if self.published_at is not None and self.publication_status != "PUBLISHED":
            raise ValueError("published_at requires PUBLISHED status")
        return self
