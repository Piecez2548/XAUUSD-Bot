"""V1B orchestration for the read-only Model Inference dataset pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.model_inference_dataset import (
    DATASET_CONTRACT_VERSION,
    FEATURE_CONTRACT_VERSION,
    DatasetManifestV1,
    DatasetRowV1,
)
from models.research_dataset_pipeline import (
    ARTIFACT_VERIFY_STAGE,
    DATA_SOURCE_STAGE,
    DATASET_ARTIFACT_KINDS,
    DATASET_AUDIT_STAGE,
    DATASET_BUILD_STAGE,
    DATASET_JSONL_ARTIFACT,
    DATASET_MANIFEST_ARTIFACT,
    DATASET_PIPELINE_STAGE_VERSION,
    DATASET_PIPELINE_STAGES,
    DATASET_PIPELINE_VERSION,
    DatasetAuditReportV1,
    DatasetBuildSummaryV1,
    DatasetPipelineStageViewV1,
    DatasetPipelineViewV1,
    DatasetSourceDescriptorV1,
)
from models.research_pipeline import EmbeddedReferenceV1, QualityGateEvidenceV1
from persistence.orm import (
    ResearchArtifactRecord,
    ResearchPipelineRunRecord,
    ResearchStageRunRecord,
)
from services.model_inference_dataset import (
    BUILDER_SEMANTICS_VERSION,
    FEATURE_KEYS,
    ModelInferenceDatasetBuilder,
    _stable,
    write_dataset_artifact,
)
from services.research_pipeline import (
    IllegalResearchTransition,
    ResearchPipelineError,
    create_pipeline_run,
    create_stage_attempt,
    mark_artifacts_validated,
    register_artifact,
    retry_stage_attempt,
    transition_pipeline,
    transition_stage,
    update_stage_evidence,
    update_stage_progress,
)

LABEL_CONTRACT_VERSION = "forward_outcome_labels_v1"
_JSONL_NAME = "model_inference_dataset_v1.jsonl"
_MANIFEST_NAME = "model_inference_dataset_v1.manifest.json"
_FORBIDDEN_FEATURE_KEYS = frozenset(
    {"gross_r", "net_r", "mfe_r", "mae_r", "terminal_timestamp", "evaluated_at"}
)


class DatasetPipelineBlocked(ResearchPipelineError):
    """The source contract or a required upstream stage is unavailable."""


class DatasetPipelineFailure(ResearchPipelineError):
    """The bounded builder or dataset audit failed."""


class DatasetPipelineReuseConflict(ResearchPipelineError):
    """A repeated request cannot silently rebuild an existing incomplete run."""


@dataclass(frozen=True)
class _BuildOutput:
    stage: ResearchStageRunRecord
    summary: DatasetBuildSummaryV1
    artifact_ids: tuple[str, str]


class _ProgressBuilder:
    """Delegate row generation while reporting exact rows observed by the builder."""

    def __init__(
        self,
        builder: ModelInferenceDatasetBuilder,
        *,
        expected_symbol: str,
        on_progress: Callable[[int], None],
    ) -> None:
        self._builder = builder
        self._expected_symbol = expected_symbol
        self._on_progress = on_progress
        self.page_size = builder.page_size
        self.session_aware_v2_accepted = builder.session_aware_v2_accepted
        self.processed = 0

    def iter_rows(self) -> Iterator[DatasetRowV1]:
        for row in self._builder.iter_rows():
            if row.symbol != self._expected_symbol:
                raise DatasetPipelineFailure("EXACT_SYMBOL_POLICY_FAILED")
            self.processed += 1
            self._on_progress(self.processed)
            yield row


def _hash_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_descriptor(*, source_logical_id: str, session_aware_v2_accepted: bool):
    if not session_aware_v2_accepted:
        raise DatasetPipelineBlocked("SESSION_AWARE_V2_SOURCE_CONTRACT_REQUIRED")
    return DatasetSourceDescriptorV1(
        dataset_contract_version=DATASET_CONTRACT_VERSION,
        feature_contract_version=FEATURE_CONTRACT_VERSION,
        label_contract_version=LABEL_CONTRACT_VERSION,
        builder_semantics_version=BUILDER_SEMANTICS_VERSION,
        source_logical_id=source_logical_id,
        symbol_policy="EXACT_SYMBOL",
        causal_cutoff_policy="CANDIDATE_DETECTED_AT_UTC",
        continuity_policy="SESSION_AWARE_V2_ACCEPTED",
    )


def _source_references(descriptor: DatasetSourceDescriptorV1):
    return (
        EmbeddedReferenceV1(
            reference_type="dataset_contract",
            reference_id=descriptor.dataset_contract_version,
        ),
        EmbeddedReferenceV1(
            reference_type="feature_contract",
            reference_id=descriptor.feature_contract_version,
        ),
        EmbeddedReferenceV1(
            reference_type="label_contract",
            reference_id=descriptor.label_contract_version,
        ),
        EmbeddedReferenceV1(
            reference_type="builder_semantics",
            reference_id=descriptor.builder_semantics_version,
        ),
        EmbeddedReferenceV1(
            reference_type="source_population",
            reference_id=descriptor.source_logical_id,
        ),
    )


def _pipeline(database, run_id: str) -> ResearchPipelineRunRecord:
    with database.session() as session:
        row = session.scalar(
            select(ResearchPipelineRunRecord).where(
                ResearchPipelineRunRecord.run_id == run_id
            )
        )
        if row is None:
            raise ResearchPipelineError("pipeline run not found")
        return row


def _latest_stage(database, pipeline_id: str, stage_key: str):
    with database.session() as session:
        return session.scalar(
            select(ResearchStageRunRecord)
            .where(
                ResearchStageRunRecord.pipeline_run_id == pipeline_id,
                ResearchStageRunRecord.stage_key == stage_key,
            )
            .order_by(ResearchStageRunRecord.attempt_number.desc())
        )


def _ensure_stage(
    database,
    *,
    pipeline_run_id: str,
    stage_key: str,
    input_references: tuple[EmbeddedReferenceV1, ...],
    evidence_references: tuple[EmbeddedReferenceV1, ...] = (),
) -> ResearchStageRunRecord:
    pipeline = _pipeline(database, pipeline_run_id)
    existing = _latest_stage(database, pipeline.id, stage_key)
    if existing is not None:
        return existing
    try:
        return create_stage_attempt(
            database,
            pipeline_run_id=pipeline_run_id,
            stage_key=stage_key,
            stage_version=DATASET_PIPELINE_STAGE_VERSION,
            attempt_number=1,
            input_references=input_references,
            evidence_references=evidence_references,
        )
    except IntegrityError:
        existing = _latest_stage(database, pipeline.id, stage_key)
        if existing is None:
            raise DatasetPipelineFailure("STAGE_IDENTITY_RACE") from None
        return existing


def _start_stage(database, stage: ResearchStageRunRecord) -> ResearchStageRunRecord:
    if stage.status != "READY":
        raise DatasetPipelineFailure("STAGE_NOT_READY")
    return transition_stage(database, stage.stage_run_id, "RUNNING")


def _finish_stage_failure(
    database, stage: ResearchStageRunRecord, reason: str, *, blocked: bool = False
) -> None:
    target = "BLOCKED" if blocked else "FAILED"
    with suppress(IllegalResearchTransition):
        transition_stage(database, stage.stage_run_id, target, reason=reason)


def _finish_pipeline_failure(database, run_id: str, reason: str, *, blocked: bool = False) -> None:
    target = "BLOCKED" if blocked else "FAILED"
    with suppress(IllegalResearchTransition):
        transition_pipeline(database, run_id, target, reason=reason)


def _source_stage(
    database,
    stage: ResearchStageRunRecord,
    descriptor: DatasetSourceDescriptorV1,
) -> bool:
    try:
        running = _start_stage(database, stage)
        gate = QualityGateEvidenceV1(
            gate_id="source_contract",
            status="PASS",
            measured={
                "dataset_contract_version": descriptor.dataset_contract_version,
                "feature_contract_version": descriptor.feature_contract_version,
                "label_contract_version": descriptor.label_contract_version,
                "builder_semantics_version": descriptor.builder_semantics_version,
                "source_logical_id": descriptor.source_logical_id,
            },
            reason_codes=("SOURCE_CONTRACT_VALID",),
        )
        update_stage_evidence(
            database,
            running.stage_run_id,
            gate_evidence=(gate,),
            lineage_references=_source_references(descriptor),
        )
        transition_stage(database, running.stage_run_id, "PASS")
        return True
    except DatasetPipelineBlocked:
        _finish_stage_failure(database, stage, "SOURCE_CONTRACT_UNAVAILABLE", blocked=True)
        return False
    except Exception:
        _finish_stage_failure(database, stage, "SOURCE_CONTRACT_VALIDATION_FAILED")
        return False


def _summary_from_manifest(manifest: DatasetManifestV1) -> DatasetBuildSummaryV1:
    audit = manifest.audit
    return DatasetBuildSummaryV1(
        candidates_inspected=audit.inspected_candidate_count,
        rows_produced=audit.row_count,
        trainable_rows=audit.trainable_count,
        non_trainable_rows=audit.non_trainable_count,
        unresolved_rows=audit.unresolved_count,
        excluded_rows=audit.builder_exclusion_count,
        artifact_logical_identities=(DATASET_JSONL_ARTIFACT, DATASET_MANIFEST_ARTIFACT),
        dataset_semantic_hash=manifest.dataset_hash,
        dataset_contract_version=manifest.dataset_contract_version,
        feature_contract_version=manifest.feature_contract_version,
        builder_semantics_version=BUILDER_SEMANTICS_VERSION,
    )


def _build_stage(
    database,
    *,
    run: ResearchPipelineRunRecord,
    stage: ResearchStageRunRecord,
    output_dir: Path,
    symbol: str,
    page_size: int,
    total_candidates: int | None,
    builder: ModelInferenceDatasetBuilder | None,
) -> _BuildOutput | None:
    try:
        running = _start_stage(database, stage)
        update_stage_progress(
            database,
            running.stage_run_id,
            processed=0,
            total=total_candidates,
            unit="candidates",
        )
        base_builder = builder or ModelInferenceDatasetBuilder(
            database, page_size=page_size, session_aware_v2_accepted=True
        )
        if not base_builder.session_aware_v2_accepted:
            raise DatasetPipelineBlocked("SESSION_AWARE_V2_SOURCE_CONTRACT_REQUIRED")
        progress_builder = _ProgressBuilder(
            base_builder,
            expected_symbol=symbol,
            on_progress=lambda count: update_stage_progress(
                database,
                running.stage_run_id,
                processed=count,
                total=total_candidates,
                unit="candidates",
            ),
        )
        attempt_dir = output_dir / run.run_id / running.stage_run_id
        manifest, artifact_path = write_dataset_artifact(progress_builder, attempt_dir)
        if total_candidates is not None and progress_builder.processed != total_candidates:
            raise DatasetPipelineFailure("BUILDER_TOTAL_MISMATCH")
        manifest_path = attempt_dir / _MANIFEST_NAME
        manifest_hash = _hash_bytes(manifest_path)
        lineage = (
            EmbeddedReferenceV1(
                reference_type="dataset_contract", reference_id=manifest.dataset_contract_version
            ),
            EmbeddedReferenceV1(
                reference_type="semantic_dataset_hash", reference_id=manifest.dataset_hash
            ),
            EmbeddedReferenceV1(
                reference_type="producer_stage_attempt", reference_id=running.stage_run_id
            ),
        )
        jsonl_record = register_artifact(
            database,
            pipeline_run_id=run.run_id,
            producer_stage_run_id=running.stage_run_id,
            artifact_id=f"{running.stage_run_id}:{DATASET_JSONL_ARTIFACT}",
            artifact_kind=DATASET_JSONL_ARTIFACT,
            content_sha256=manifest.artifact_sha256,
            manifest_sha256=manifest_hash,
            artifact_format="jsonl",
            size_bytes=artifact_path.stat().st_size,
            logical_locator=DATASET_JSONL_ARTIFACT,
            lineage_references=lineage,
        )
        manifest_record = register_artifact(
            database,
            pipeline_run_id=run.run_id,
            producer_stage_run_id=running.stage_run_id,
            artifact_id=f"{running.stage_run_id}:{DATASET_MANIFEST_ARTIFACT}",
            artifact_kind=DATASET_MANIFEST_ARTIFACT,
            content_sha256=manifest_hash,
            artifact_format="json",
            size_bytes=manifest_path.stat().st_size,
            logical_locator=DATASET_MANIFEST_ARTIFACT,
            lineage_references=lineage,
        )
        summary = _summary_from_manifest(manifest)
        gate = QualityGateEvidenceV1(
            gate_id="dataset_build_summary",
            status="PASS",
            measured={
                "candidates_inspected": summary.candidates_inspected,
                "rows_produced": summary.rows_produced,
                "trainable_rows": summary.trainable_rows,
                "non_trainable_rows": summary.non_trainable_rows,
                "unresolved_rows": summary.unresolved_rows,
                "excluded_rows": summary.excluded_rows,
                "dataset_semantic_hash": summary.dataset_semantic_hash,
                "dataset_contract_version": summary.dataset_contract_version,
                "feature_contract_version": summary.feature_contract_version,
                "builder_semantics_version": summary.builder_semantics_version,
                "artifact_jsonl": DATASET_JSONL_ARTIFACT,
                "artifact_manifest": DATASET_MANIFEST_ARTIFACT,
            },
            reason_codes=("DATASET_BUILD_COMPLETED",),
        )
        update_stage_evidence(
            database,
            running.stage_run_id,
            gate_evidence=(gate,),
            lineage_references=lineage,
        )
        transition_stage(database, running.stage_run_id, "PASS")
        return _BuildOutput(
            stage=running,
            summary=summary,
            artifact_ids=(jsonl_record.artifact_id, manifest_record.artifact_id),
        )
    except DatasetPipelineBlocked:
        _finish_stage_failure(database, stage, "SOURCE_CONTRACT_UNAVAILABLE", blocked=True)
        return None
    except Exception:
        _finish_stage_failure(database, stage, "BUILDER_EXCEPTION")
        return None


def _audit_files(
    jsonl_path: Path,
    manifest_path: Path,
    *,
    expected_artifact_sha256: str,
    expected_manifest_sha256: str,
    expected_symbol: str,
) -> DatasetAuditReportV1:
    summary = DatasetBuildSummaryV1(
        candidates_inspected=0,
        rows_produced=0,
        trainable_rows=0,
        non_trainable_rows=0,
        unresolved_rows=0,
        excluded_rows=0,
        artifact_logical_identities=(DATASET_JSONL_ARTIFACT, DATASET_MANIFEST_ARTIFACT),
        dataset_semantic_hash="0" * 64,
        dataset_contract_version=DATASET_CONTRACT_VERSION,
        feature_contract_version=FEATURE_CONTRACT_VERSION,
        builder_semantics_version=BUILDER_SEMANTICS_VERSION,
    )
    failure_codes: list[str] = []
    artifact_readable = manifest_readable = False
    artifact_sha_matches = manifest_sha_matches = False
    semantic_hash_matches = row_count_matches = False
    fingerprints_valid = canonical_valid = versions_valid = False
    training_valid = leakage_valid = exact_symbol_valid = continuity_valid = False
    summary_counts_match = False
    manifest: DatasetManifestV1 | None = None
    raw_artifact = b""
    try:
        raw_artifact = jsonl_path.read_bytes()
        artifact_readable = True
        artifact_sha_matches = hashlib.sha256(raw_artifact).hexdigest() == expected_artifact_sha256
    except (OSError, ValueError):
        failure_codes.append("ARTIFACT_UNREADABLE")
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest_readable = True
        manifest_sha_matches = hashlib.sha256(raw_manifest).hexdigest() == expected_manifest_sha256
        manifest = DatasetManifestV1.model_validate(json.loads(raw_manifest.decode("utf-8")))
        summary = _summary_from_manifest(manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        failure_codes.append("MANIFEST_UNREADABLE")

    rows: list[DatasetRowV1] = []
    if artifact_readable and manifest is not None:
        try:
            lines = raw_artifact.splitlines(keepends=True)
            canonical_valid = True
            fingerprints_valid = True
            identity_values: set[str] = set()
            candidate_values: set[str] = set()
            fingerprint_values: set[str] = set()
            semantic_hasher = hashlib.sha256()
            semantic_header = {
                "dataset_contract_version": DATASET_CONTRACT_VERSION,
                "feature_contract_version": FEATURE_CONTRACT_VERSION,
                "builder_semantics_version": BUILDER_SEMANTICS_VERSION,
                "bounded_page_size": manifest.bounded_page_size,
                "session_aware_v2_accepted": manifest.session_aware_v2_accepted,
                "artifact_format": manifest.artifact_format,
            }
            semantic_hasher.update(_stable(semantic_header).encode("utf-8"))
            semantic_hasher.update(b"\n")
            for line in lines:
                row = DatasetRowV1.model_validate(json.loads(line.decode("utf-8")))
                rows.append(row)
                canonical_line = (_stable(row.model_dump(mode="json")) + "\n").encode("utf-8")
                canonical_valid = canonical_valid and line == canonical_line
                payload = row.model_dump(mode="json")
                actual_fingerprint = payload.pop("row_fingerprint")
                fingerprints_valid = fingerprints_valid and hashlib.sha256(
                    _stable(payload).encode("utf-8")
                ).hexdigest() == actual_fingerprint
                identity_values.add(row.row_identity)
                candidate_values.add(row.candidate_id)
                fingerprint_values.add(row.row_fingerprint)
                semantic_hasher.update(f"{row.row_identity}:{row.row_fingerprint}\n".encode("ascii"))
            semantic_hash_matches = semantic_hasher.hexdigest() == manifest.dataset_hash
            row_count_matches = len(rows) == manifest.row_count == manifest.audit.row_count
            duplicate_count = len(rows) - len(identity_values)
            duplicate_candidate_count = len(rows) - len(candidate_values)
            duplicate_fingerprint_count = len(rows) - len(fingerprint_values)
            summary_counts_match = (
                manifest.audit.row_count == len(rows)
                and manifest.audit.trainable_count
                == sum(row.training_eligibility == "TRAINABLE" for row in rows)
                and manifest.audit.non_trainable_count
                == sum(row.training_eligibility == "NON_TRAINABLE" for row in rows)
                and manifest.audit.unresolved_count
                == sum(row.training_eligibility == "UNRESOLVED" for row in rows)
            )
            versions_valid = all(
                row.dataset_contract_version == DATASET_CONTRACT_VERSION
                and row.feature_contract_version == FEATURE_CONTRACT_VERSION
                for row in rows
            ) and manifest.dataset_contract_version == DATASET_CONTRACT_VERSION
            exact_symbol_valid = all(row.symbol == expected_symbol for row in rows)
            continuity_valid = manifest.session_aware_v2_accepted and all(
                "SESSION_AWARE_V2_NOT_ACCEPTED" not in row.reason_codes for row in rows
            )
            leakage_valid = all(
                set(row.features).issubset(FEATURE_KEYS)
                and not set(row.features).intersection(_FORBIDDEN_FEATURE_KEYS)
                for row in rows
            )
            training_valid = all(
                row.training_eligibility != "TRAINABLE"
                or (
                    row.state == "OUTCOME_COMPLETED"
                    and row.outcome_label in {"TP", "SL"}
                    and not row.reason_codes
                )
                for row in rows
            )
            if duplicate_count or duplicate_candidate_count:
                failure_codes.append("DUPLICATE_CANONICAL_CANDIDATE_IDENTITY")
            if duplicate_fingerprint_count:
                failure_codes.append("DUPLICATE_ROW_FINGERPRINT")
            if not semantic_hash_matches:
                failure_codes.append("DATASET_SEMANTIC_HASH_MISMATCH")
            if not row_count_matches:
                failure_codes.append("MANIFEST_ROW_COUNT_MISMATCH")
            if not summary_counts_match:
                failure_codes.append("SUMMARY_COUNTS_MISMATCH")
            if not fingerprints_valid:
                failure_codes.append("ROW_FINGERPRINT_INVALID")
            if not canonical_valid:
                failure_codes.append("CANONICAL_SERIALIZATION_INVALID")
            if not versions_valid:
                failure_codes.append("CONTRACT_VERSION_MISMATCH")
            if not exact_symbol_valid:
                failure_codes.append("EXACT_SYMBOL_POLICY_FAILED")
            if not continuity_valid:
                failure_codes.append("CONTINUITY_POLICY_FAILED")
            if not leakage_valid:
                failure_codes.append("FEATURE_LABEL_LEAKAGE")
            if not training_valid:
                failure_codes.append("TRAINING_ELIGIBILITY_CONTRADICTION")
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            failure_codes.append("ARTIFACT_ROW_INVALID")
    if not artifact_sha_matches:
        failure_codes.append("ARTIFACT_SHA256_MISMATCH")
    if not manifest_sha_matches:
        failure_codes.append("MANIFEST_SHA256_MISMATCH")
    return DatasetAuditReportV1(
        artifact_readable=artifact_readable,
        manifest_readable=manifest_readable,
        artifact_sha256_matches=artifact_sha_matches,
        manifest_sha256_matches=manifest_sha_matches,
        semantic_hash_matches=semantic_hash_matches,
        row_count_matches=row_count_matches,
        row_fingerprints_valid=fingerprints_valid,
        canonical_ordering_valid=canonical_valid,
        contract_versions_valid=versions_valid,
        duplicate_candidate_identities=(
            len(rows) - len({row.candidate_id for row in rows}) if rows else 0
        ),
        duplicate_row_fingerprints=(
            len(rows) - len({row.row_fingerprint for row in rows}) if rows else 0
        ),
        summary_counts_match=summary_counts_match,
        training_eligibility_consistent=training_valid,
        leakage_checks_passed=leakage_valid,
        exact_symbol_policy_satisfied=exact_symbol_valid,
        continuity_policy_satisfied=continuity_valid,
        summary=summary,
        failure_codes=tuple(sorted(set(failure_codes))),
    )


def _artifact_rows(database, stage_id: str) -> tuple[ResearchArtifactRecord, ...]:
    with database.session() as session:
        return tuple(
            session.scalars(
                select(ResearchArtifactRecord)
                .where(ResearchArtifactRecord.producer_stage_run_id == stage_id)
                .order_by(ResearchArtifactRecord.artifact_kind)
            ).all()
        )


def _build_output_for_stage(database, stage: ResearchStageRunRecord) -> _BuildOutput:
    artifacts = _artifact_rows(database, stage.id)
    by_kind = {row.artifact_kind: row for row in artifacts}
    if set(by_kind) != set(DATASET_ARTIFACT_KINDS):
        raise DatasetPipelineFailure("REQUIRED_ARTIFACTS_MISSING")
    summary = None
    for item in stage.gate_evidence_json or []:
        if item.get("gate_id") != "dataset_build_summary" or item.get("status") != "PASS":
            continue
        measured = item.get("measured", {})
        try:
            summary = DatasetBuildSummaryV1(
                candidates_inspected=measured["candidates_inspected"],
                rows_produced=measured["rows_produced"],
                trainable_rows=measured["trainable_rows"],
                non_trainable_rows=measured["non_trainable_rows"],
                unresolved_rows=measured["unresolved_rows"],
                excluded_rows=measured["excluded_rows"],
                artifact_logical_identities=(
                    measured["artifact_jsonl"],
                    measured["artifact_manifest"],
                ),
                dataset_semantic_hash=measured["dataset_semantic_hash"],
                dataset_contract_version=measured["dataset_contract_version"],
                feature_contract_version=measured["feature_contract_version"],
                builder_semantics_version=measured["builder_semantics_version"],
            )
        except (KeyError, TypeError, ValueError):
            raise DatasetPipelineFailure("BUILD_SUMMARY_UNAVAILABLE") from None
        break
    if summary is None:
        raise DatasetPipelineFailure("BUILD_SUMMARY_UNAVAILABLE")
    return _BuildOutput(
        stage=stage,
        summary=summary,
        artifact_ids=(
            by_kind[DATASET_JSONL_ARTIFACT].artifact_id,
            by_kind[DATASET_MANIFEST_ARTIFACT].artifact_id,
        ),
    )


def _audit_stage(
    database,
    *,
    run: ResearchPipelineRunRecord,
    build: _BuildOutput,
    stage: ResearchStageRunRecord,
    output_dir: Path,
    symbol: str,
) -> bool:
    try:
        running = _start_stage(database, stage)
        artifacts = _artifact_rows(database, build.stage.id)
        by_kind = {row.artifact_kind: row for row in artifacts}
        if set(by_kind) != set(DATASET_ARTIFACT_KINDS):
            raise DatasetPipelineFailure("REQUIRED_ARTIFACTS_MISSING")
        attempt_dir = output_dir / run.run_id / build.stage.stage_run_id
        report = _audit_files(
            attempt_dir / _JSONL_NAME,
            attempt_dir / _MANIFEST_NAME,
            expected_artifact_sha256=by_kind[DATASET_JSONL_ARTIFACT].content_sha256,
            expected_manifest_sha256=by_kind[DATASET_JSONL_ARTIFACT].manifest_sha256 or "",
            expected_symbol=symbol,
        )
        gate = QualityGateEvidenceV1(
            gate_id="dataset_contract_audit",
            status="PASS" if report.passed else "FAILED",
            measured={
                "artifact_readable": report.artifact_readable,
                "manifest_readable": report.manifest_readable,
                "artifact_sha256_matches": report.artifact_sha256_matches,
                "manifest_sha256_matches": report.manifest_sha256_matches,
                "semantic_hash_matches": report.semantic_hash_matches,
                "row_count_matches": report.row_count_matches,
                "row_fingerprints_valid": report.row_fingerprints_valid,
                "canonical_ordering_valid": report.canonical_ordering_valid,
                "contract_versions_valid": report.contract_versions_valid,
                "duplicate_row_fingerprints": report.duplicate_row_fingerprints,
                "summary_counts_match": report.summary_counts_match,
                "training_eligibility_consistent": report.training_eligibility_consistent,
                "leakage_checks_passed": report.leakage_checks_passed,
                "exact_symbol_policy_satisfied": report.exact_symbol_policy_satisfied,
                "continuity_policy_satisfied": report.continuity_policy_satisfied,
                "duplicate_candidate_identities": report.duplicate_candidate_identities,
            },
            reason_codes=report.failure_codes or ("DATASET_AUDIT_PASSED",),
        )
        update_stage_evidence(
            database,
            running.stage_run_id,
            gate_evidence=(gate,),
            lineage_references=(
                EmbeddedReferenceV1(
                    reference_type="dataset_semantic_hash",
                    reference_id=report.summary.dataset_semantic_hash,
                ),
                EmbeddedReferenceV1(
                    reference_type="producer_stage_attempt",
                    reference_id=build.stage.stage_run_id,
                ),
            ),
        )
        if not report.passed:
            transition_stage(
                database,
                running.stage_run_id,
                "FAILED",
                reason="DATASET_AUDIT_FAILED",
            )
            return False
        mark_artifacts_validated(database, tuple(row.artifact_id for row in artifacts))
        transition_stage(database, running.stage_run_id, "PASS")
        return True
    except Exception:
        _finish_stage_failure(database, stage, "DATASET_AUDIT_FAILED")
        return False


def _verify_stage(
    database,
    *,
    stage: ResearchStageRunRecord,
    build: _BuildOutput,
    audit: ResearchStageRunRecord,
) -> bool:
    try:
        running = _start_stage(database, stage)
        artifacts = _artifact_rows(database, build.stage.id)
        if len(artifacts) != 2 or any(
            row.validation_status != "VALID" or row.publication_status != "PUBLISHED"
            for row in artifacts
        ):
            raise DatasetPipelineFailure("ARTIFACT_NOT_VALIDATED")
        gate = QualityGateEvidenceV1(
            gate_id="artifact_verification",
            status="PASS",
            measured={
                "verified_artifacts": len(artifacts),
                "dataset_jsonl": DATASET_JSONL_ARTIFACT,
                "dataset_manifest": DATASET_MANIFEST_ARTIFACT,
            },
            reason_codes=("ARTIFACTS_VERIFIED",),
        )
        update_stage_evidence(
            database,
            running.stage_run_id,
            gate_evidence=(gate,),
            lineage_references=(
                EmbeddedReferenceV1(
                    reference_type="dataset_audit_stage", reference_id=audit.stage_run_id
                ),
                EmbeddedReferenceV1(
                    reference_type="producer_stage_attempt", reference_id=build.stage.stage_run_id
                ),
            ),
        )
        transition_stage(database, running.stage_run_id, "PASS")
        return True
    except Exception:
        _finish_stage_failure(database, stage, "ARTIFACT_VERIFICATION_FAILED")
        return False


def _execute_from_source(
    database,
    *,
    run: ResearchPipelineRunRecord,
    output_dir: Path,
    symbol: str,
    source_logical_id: str,
    page_size: int,
    total_candidates: int | None,
    builder: ModelInferenceDatasetBuilder | None,
    source_stage: ResearchStageRunRecord,
) -> DatasetPipelineViewV1:
    descriptor = _source_descriptor(
        source_logical_id=source_logical_id, session_aware_v2_accepted=True
    )
    if not _source_stage(database, source_stage, descriptor):
        _finish_pipeline_failure(database, run.run_id, "SOURCE_CONTRACT_UNAVAILABLE", blocked=True)
        return get_dataset_pipeline_view(database, run.run_id)
    build_stage = _ensure_stage(
        database,
        pipeline_run_id=run.run_id,
        stage_key=DATASET_BUILD_STAGE,
        input_references=_source_references(descriptor),
    )
    build = _build_stage(
        database,
        run=run,
        stage=build_stage,
        output_dir=output_dir,
        symbol=symbol,
        page_size=page_size,
        total_candidates=total_candidates,
        builder=builder,
    )
    if build is None:
        failed_build = _latest_stage(database, run.id, DATASET_BUILD_STAGE)
        blocked = failed_build is not None and failed_build.status == "BLOCKED"
        _finish_pipeline_failure(
            database,
            run.run_id,
            "SOURCE_CONTRACT_UNAVAILABLE" if blocked else "BUILDER_EXCEPTION",
            blocked=blocked,
        )
        return get_dataset_pipeline_view(database, run.run_id)
    audit_stage = _ensure_stage(
        database,
        pipeline_run_id=run.run_id,
        stage_key=DATASET_AUDIT_STAGE,
        input_references=(
            EmbeddedReferenceV1(
                reference_type="dataset_build_stage", reference_id=build.stage.stage_run_id
            ),
        ),
    )
    if not _audit_stage(
        database,
        run=run,
        build=build,
        stage=audit_stage,
        output_dir=output_dir,
        symbol=symbol,
    ):
        _finish_pipeline_failure(database, run.run_id, "DATASET_AUDIT_FAILED")
        return get_dataset_pipeline_view(database, run.run_id)
    verify_stage = _ensure_stage(
        database,
        pipeline_run_id=run.run_id,
        stage_key=ARTIFACT_VERIFY_STAGE,
        input_references=(
            EmbeddedReferenceV1(
                reference_type="dataset_audit_stage", reference_id=audit_stage.stage_run_id
            ),
        ),
    )
    audit_latest = _latest_stage(database, run.id, DATASET_AUDIT_STAGE)
    if audit_latest is None or not _verify_stage(
        database, stage=verify_stage, build=build, audit=audit_latest
    ):
        _finish_pipeline_failure(database, run.run_id, "ARTIFACT_VERIFICATION_FAILED")
        return get_dataset_pipeline_view(database, run.run_id)
    transition_pipeline(database, run.run_id, "PASS")
    return get_dataset_pipeline_view(database, run.run_id)


def run_model_inference_dataset_pipeline(
    database,
    *,
    idempotency_key: str,
    output_dir: Path,
    symbol: str = "XAUUSD",
    source_logical_id: str = "strategy_intelligence_forward_evidence_v1",
    page_size: int = 100,
    total_candidates: int | None = None,
    builder: ModelInferenceDatasetBuilder | None = None,
) -> DatasetPipelineViewV1:
    """Run or reuse one bounded, read-only Dataset Foundation pipeline."""

    if total_candidates is not None and total_candidates < 0:
        raise ValueError("total_candidates must be non-negative")
    descriptor = _source_descriptor(
        source_logical_id=source_logical_id, session_aware_v2_accepted=True
    )
    source_refs = _source_references(descriptor)
    run = create_pipeline_run(
        database,
        pipeline_version=DATASET_PIPELINE_VERSION,
        experiment_key=DATASET_PIPELINE_VERSION,
        idempotency_key=idempotency_key,
        requested_inputs={
            "symbol": symbol,
            "source_logical_id": source_logical_id,
            "dataset_contract_version": DATASET_CONTRACT_VERSION,
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "label_contract_version": LABEL_CONTRACT_VERSION,
            "builder_semantics_version": BUILDER_SEMANTICS_VERSION,
            "session_aware_v2_accepted": True,
            "page_size": page_size,
            "total_candidates": total_candidates,
        },
        source_references=source_refs,
        configuration_references=(
            EmbeddedReferenceV1(
                reference_type="continuity_policy", reference_id="session_aware_v2_accepted"
            ),
        ),
        code_references=(
            EmbeddedReferenceV1(
                reference_type="pipeline_definition", reference_id=DATASET_PIPELINE_VERSION
            ),
        ),
    )
    if run.status == "PASS":
        return get_dataset_pipeline_view(database, run.run_id)
    if run.status != "WAITING":
        return get_dataset_pipeline_view(database, run.run_id)
    source_stage = _ensure_stage(
        database,
        pipeline_run_id=run.run_id,
        stage_key=DATA_SOURCE_STAGE,
        input_references=source_refs,
    )
    try:
        transition_pipeline(database, run.run_id, "READY")
        transition_pipeline(database, run.run_id, "RUNNING")
    except Exception:
        return get_dataset_pipeline_view(database, run.run_id)
    return _execute_from_source(
        database,
        run=run,
        output_dir=Path(output_dir),
        symbol=symbol,
        source_logical_id=source_logical_id,
        page_size=page_size,
        total_candidates=total_candidates,
        builder=builder,
        source_stage=source_stage,
    )


def retry_model_inference_dataset_pipeline(
    database,
    *,
    run_id: str,
    output_dir: Path,
    builder: ModelInferenceDatasetBuilder | None = None,
) -> DatasetPipelineViewV1:
    """Retry the first failed stage as a new immutable attempt."""

    run = _pipeline(database, run_id)
    if run.status not in {"FAILED", "BLOCKED"}:
        raise DatasetPipelineReuseConflict("only failed or blocked pipelines can be retried")
    with database.session() as session:
        failed = session.scalar(
            select(ResearchStageRunRecord)
            .where(
                ResearchStageRunRecord.pipeline_run_id == run.id,
                ResearchStageRunRecord.status.in_(("FAILED", "BLOCKED")),
            )
            .order_by(ResearchStageRunRecord.attempt_number.desc())
        )
    if failed is None:
        raise DatasetPipelineReuseConflict("pipeline has no retryable stage")
    transition_pipeline(database, run_id, "READY")
    retry = retry_stage_attempt(database, failed.stage_run_id)
    transition_pipeline(database, run_id, "RUNNING")
    requested = run.requested_inputs_json
    symbol = str(requested.get("symbol", "XAUUSD"))
    page_size = int(requested.get("page_size", 100))
    total = requested.get("total_candidates")
    if failed.stage_key == DATASET_BUILD_STAGE:
        build = _build_stage(
            database,
            run=run,
            stage=retry,
            output_dir=Path(output_dir),
            symbol=symbol,
            page_size=page_size,
            total_candidates=total,
            builder=builder,
        )
        if build is None:
            latest_build = _latest_stage(database, run.id, DATASET_BUILD_STAGE)
            blocked = latest_build is not None and latest_build.status == "BLOCKED"
            _finish_pipeline_failure(
                database,
                run_id,
                "SOURCE_CONTRACT_UNAVAILABLE" if blocked else "BUILDER_EXCEPTION",
                blocked=blocked,
            )
            return get_dataset_pipeline_view(database, run_id)
        source = _latest_stage(database, run.id, DATA_SOURCE_STAGE)
        if source is None or source.status != "PASS":
            _finish_pipeline_failure(database, run_id, "SOURCE_CONTRACT_UNAVAILABLE", blocked=True)
            return get_dataset_pipeline_view(database, run_id)
        audit = _ensure_stage(
            database,
            pipeline_run_id=run_id,
            stage_key=DATASET_AUDIT_STAGE,
            input_references=(
                EmbeddedReferenceV1(
                    reference_type="dataset_build_stage", reference_id=build.stage.stage_run_id
                ),
            ),
        )
        if not _audit_stage(
            database,
            run=run,
            build=build,
            stage=audit,
            output_dir=Path(output_dir),
            symbol=symbol,
        ):
            _finish_pipeline_failure(database, run_id, "DATASET_AUDIT_FAILED")
            return get_dataset_pipeline_view(database, run_id)
        verify = _ensure_stage(
            database,
            pipeline_run_id=run_id,
            stage_key=ARTIFACT_VERIFY_STAGE,
            input_references=(
                EmbeddedReferenceV1(
                    reference_type="dataset_audit_stage", reference_id=audit.stage_run_id
                ),
            ),
        )
        if not _verify_stage(database, stage=verify, build=build, audit=audit):
            _finish_pipeline_failure(database, run_id, "ARTIFACT_VERIFICATION_FAILED")
            return get_dataset_pipeline_view(database, run_id)
        transition_pipeline(database, run_id, "PASS")
        return get_dataset_pipeline_view(database, run_id)
    if failed.stage_key == DATASET_AUDIT_STAGE:
        build_stage = _latest_stage(database, run.id, DATASET_BUILD_STAGE)
        if build_stage is None or build_stage.status != "PASS":
            raise DatasetPipelineReuseConflict("a passed Dataset Build is required")
        build = _build_output_for_stage(database, build_stage)
        audit = retry
        if not _audit_stage(
            database,
            run=run,
            build=build,
            stage=audit,
            output_dir=Path(output_dir),
            symbol=symbol,
        ):
            _finish_pipeline_failure(database, run_id, "DATASET_AUDIT_FAILED")
            return get_dataset_pipeline_view(database, run_id)
        verify = _ensure_stage(
            database,
            pipeline_run_id=run_id,
            stage_key=ARTIFACT_VERIFY_STAGE,
            input_references=(
                EmbeddedReferenceV1(
                    reference_type="dataset_audit_stage", reference_id=audit.stage_run_id
                ),
            ),
        )
        if not _verify_stage(database, stage=verify, build=build, audit=audit):
            _finish_pipeline_failure(database, run_id, "ARTIFACT_VERIFICATION_FAILED")
            return get_dataset_pipeline_view(database, run_id)
        transition_pipeline(database, run_id, "PASS")
        return get_dataset_pipeline_view(database, run_id)
    if failed.stage_key == ARTIFACT_VERIFY_STAGE:
        build_stage = _latest_stage(database, run.id, DATASET_BUILD_STAGE)
        audit_stage = _latest_stage(database, run.id, DATASET_AUDIT_STAGE)
        if (
            build_stage is None
            or build_stage.status != "PASS"
            or audit_stage is None
            or audit_stage.status != "PASS"
        ):
            raise DatasetPipelineReuseConflict("passed build and audit are required")
        build = _build_output_for_stage(database, build_stage)
        if not _verify_stage(database, stage=retry, build=build, audit=audit_stage):
            _finish_pipeline_failure(database, run_id, "ARTIFACT_VERIFICATION_FAILED")
            return get_dataset_pipeline_view(database, run_id)
        transition_pipeline(database, run_id, "PASS")
        return get_dataset_pipeline_view(database, run_id)
    raise DatasetPipelineReuseConflict("stage is not retryable")


def get_dataset_pipeline_view(database, run_id: str) -> DatasetPipelineViewV1:
    """Return a bounded roadmap view with no filesystem or database paths."""

    with database.session() as session:
        run = session.scalar(
            select(ResearchPipelineRunRecord).where(
                ResearchPipelineRunRecord.run_id == run_id
            )
        )
        if run is None:
            raise ResearchPipelineError("pipeline run not found")
        stages = list(
            session.scalars(
                select(ResearchStageRunRecord)
                .where(ResearchStageRunRecord.pipeline_run_id == run.id)
                .order_by(ResearchStageRunRecord.stage_key, ResearchStageRunRecord.attempt_number)
            ).all()
        )
        stages.sort(
            key=lambda item: (
                DATASET_PIPELINE_STAGES.index(item.stage_key),
                item.attempt_number,
            )
        )
        artifacts = list(
            session.scalars(
                select(ResearchArtifactRecord)
                .where(ResearchArtifactRecord.pipeline_run_id == run.id)
                .order_by(ResearchArtifactRecord.artifact_kind, ResearchArtifactRecord.artifact_id)
            ).all()
        )
        stage_by_id = {stage.id: stage for stage in stages}
        stage_views = tuple(
            DatasetPipelineStageViewV1(
                stage_run_id=stage.stage_run_id,
                stage_key=stage.stage_key,
                attempt_number=stage.attempt_number,
                status=stage.status,
                processed=stage.progress_processed,
                total=stage.progress_total,
                unit=stage.progress_unit,
                started_at=None if stage.started_at is None else stage.started_at.isoformat(),
                completed_at=None
                if stage.completed_at is None
                else stage.completed_at.isoformat(),
                reason_codes=tuple(
                    value
                    for value in (
                        stage.blocked_reason_code,
                        stage.failure_reason_code,
                        stage.terminal_reason,
                    )
                    if value
                ),
                gate_evidence=tuple(
                    QualityGateEvidenceV1.model_validate(item)
                    for item in (stage.gate_evidence_json or [])
                ),
                lineage_references=tuple(
                    EmbeddedReferenceV1.model_validate(item)
                    for item in (stage.lineage_references_json or [])
                ),
            )
            for stage in stages
        )
        artifact_views = tuple(
            _artifact_view(artifact, stage_by_id[artifact.producer_stage_run_id])
            for artifact in artifacts
            if artifact.producer_stage_run_id in stage_by_id
        )
        summary = None
        for stage in reversed(stages):
            for item in reversed(stage.gate_evidence_json or []):
                if item.get("gate_id") == "dataset_build_summary" and item.get("status") == "PASS":
                    measured = item.get("measured", {})
                    try:
                        summary = DatasetBuildSummaryV1(
                            candidates_inspected=measured["candidates_inspected"],
                            rows_produced=measured["rows_produced"],
                            trainable_rows=measured["trainable_rows"],
                            non_trainable_rows=measured["non_trainable_rows"],
                            unresolved_rows=measured["unresolved_rows"],
                            excluded_rows=measured["excluded_rows"],
                            artifact_logical_identities=(
                                measured["artifact_jsonl"],
                                measured["artifact_manifest"],
                            ),
                            dataset_semantic_hash=measured["dataset_semantic_hash"],
                            dataset_contract_version=measured["dataset_contract_version"],
                            feature_contract_version=measured["feature_contract_version"],
                            builder_semantics_version=measured["builder_semantics_version"],
                        )
                    except (KeyError, TypeError, ValueError):
                        summary = None
                    break
            if summary is not None:
                break
        return DatasetPipelineViewV1(
            run_id=run.run_id,
            pipeline_version=run.pipeline_version,
            status=run.status,
            created_at=run.created_at.isoformat(),
            started_at=None if run.started_at is None else run.started_at.isoformat(),
            completed_at=None if run.completed_at is None else run.completed_at.isoformat(),
            stages=stage_views,
            artifacts=artifact_views,
            summary=summary,
        )


def _artifact_view(artifact: ResearchArtifactRecord, stage: ResearchStageRunRecord):
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": artifact.artifact_kind,
        "logical_locator": artifact.logical_locator,
        "content_sha256": artifact.content_sha256,
        "validation_status": artifact.validation_status,
        "publication_status": artifact.publication_status,
        "producer_stage_run_id": stage.stage_run_id,
        "producer_attempt_number": stage.attempt_number,
        "lineage_references": tuple(
            EmbeddedReferenceV1.model_validate(item)
            for item in (artifact.lineage_references_json or [])
        ),
    }
