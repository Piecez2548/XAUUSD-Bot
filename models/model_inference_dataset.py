"""Frozen contracts for the read-only Model Inference V2 dataset foundation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
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


_DATASET_ROW_FINGERPRINT_FIELDS = frozenset(DatasetRowV1.model_fields) - {
    "row_fingerprint"
}


def _canonical_fingerprint_value(value: Any, *, timestamp: bool = False) -> Any:
    """Normalize supported values before canonical JSON serialization."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dataset row timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str) and timestamp:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("dataset row timestamps must be valid ISO-8601 values") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("dataset row timestamps must be timezone-aware")
        return parsed.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_fingerprint_value(item, timestamp=timestamp)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_canonical_fingerprint_value(item, timestamp=timestamp) for item in value]
    return value


def canonical_dataset_row_fingerprint_payload(
    row: DatasetRowV1 | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact semantic object used for row fingerprinting.

    The optional ``row_fingerprint`` field is removed before canonicalization,
    so the fingerprint cannot recursively influence itself.  Only the frozen
    DatasetRowV1 fields are accepted; physical paths, build timestamps, and
    other metadata therefore cannot enter the fingerprint contract.
    """

    if isinstance(row, DatasetRowV1):
        payload = row.model_dump(mode="python")
    elif isinstance(row, Mapping):
        payload = dict(row)
    else:
        raise TypeError("dataset row fingerprint input must be a DatasetRowV1 or mapping")
    payload.pop("row_fingerprint", None)
    fields = set(payload)
    extra = fields - _DATASET_ROW_FINGERPRINT_FIELDS
    missing = _DATASET_ROW_FINGERPRINT_FIELDS - fields
    if extra or missing:
        raise ValueError("dataset row fingerprint payload fields do not match DatasetRowV1")
    timestamp_fields = {"candidate_timestamp", "causal_cutoff_timestamp", "source_timestamps"}
    return {
        key: _canonical_fingerprint_value(value, timestamp=key in timestamp_fields)
        for key, value in payload.items()
    }


def dataset_row_fingerprint(row: DatasetRowV1 | Mapping[str, Any]) -> str:
    """Hash the canonical semantic DatasetRowV1 payload with SHA-256."""

    payload = canonical_dataset_row_fingerprint_payload(row)
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


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
