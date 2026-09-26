"""Persistence and state-machine helpers for the read-only research foundation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from models.research_pipeline import (
    ARTIFACT_CONTRACT_VERSION,
    PIPELINE_RUN_CONTRACT_VERSION,
    STAGE_RUN_CONTRACT_VERSION,
    EmbeddedReferenceV1,
    QualityGateEvidenceV1,
    ResearchArtifact,
    ResearchPipelineRun,
    ResearchStageRun,
    ResearchStatus,
)
from persistence.orm import (
    ResearchArtifactRecord,
    ResearchPipelineRunRecord,
    ResearchStageRunRecord,
)

LEGAL_TRANSITIONS: dict[ResearchStatus, frozenset[ResearchStatus]] = {
    "WAITING": frozenset({"READY", "BLOCKED", "SKIPPED", "CANCELLED"}),
    "READY": frozenset({"RUNNING", "BLOCKED", "CANCELLED"}),
    "RUNNING": frozenset({"PASS", "FAILED", "BLOCKED", "CANCELLED"}),
    "BLOCKED": frozenset({"READY", "CANCELLED"}),
    "FAILED": frozenset({"READY", "CANCELLED"}),
    "PASS": frozenset(),
    "SKIPPED": frozenset(),
    "CANCELLED": frozenset(),
}


class ResearchPipelineError(RuntimeError):
    """Base exception for deterministic research-foundation failures."""


class IllegalResearchTransition(ResearchPipelineError):
    pass


class TransitionConflict(ResearchPipelineError):
    """The persisted status changed after the caller validated its transition."""


class IdempotencyConflict(ResearchPipelineError):
    pass


class ArtifactIdentityConflict(ResearchPipelineError):
    pass


class ArtifactContentConflict(ResearchPipelineError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(
    *,
    pipeline_version: str,
    experiment_key: str,
    requested_inputs: dict[str, Any],
    source_references: Iterable[EmbeddedReferenceV1],
    configuration_references: Iterable[EmbeddedReferenceV1],
    code_references: Iterable[EmbeddedReferenceV1],
) -> str:
    payload = {
        "pipeline_version": pipeline_version,
        "experiment_key": experiment_key,
        "requested_inputs": requested_inputs,
        "source_references": [item.model_dump(mode="json") for item in source_references],
        "configuration_references": [
            item.model_dump(mode="json") for item in configuration_references
        ],
        "code_references": [item.model_dump(mode="json") for item in code_references],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _same_idempotent_request(row: ResearchPipelineRunRecord, fingerprint: str) -> bool:
    return row.request_fingerprint == fingerprint


def create_pipeline_run(
    database,
    *,
    pipeline_version: str,
    experiment_key: str,
    idempotency_key: str,
    requested_inputs: dict[str, Any] | None = None,
    source_references: tuple[EmbeddedReferenceV1, ...] = (),
    configuration_references: tuple[EmbeddedReferenceV1, ...] = (),
    code_references: tuple[EmbeddedReferenceV1, ...] = (),
    run_id: str | None = None,
) -> ResearchPipelineRunRecord:
    """Create or reuse a pipeline by idempotency key using a DB uniqueness guard."""

    inputs = requested_inputs or {}
    fingerprint = _fingerprint(
        pipeline_version=pipeline_version,
        experiment_key=experiment_key,
        requested_inputs=inputs,
        source_references=source_references,
        configuration_references=configuration_references,
        code_references=code_references,
    )
    now = _utc_now()
    contract = ResearchPipelineRun(
        run_id=run_id or str(uuid4()),
        pipeline_version=pipeline_version,
        experiment_key=experiment_key,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        requested_inputs=inputs,
        source_references=source_references,
        configuration_references=configuration_references,
        code_references=code_references,
        created_at=now,
    )
    values = contract.model_dump(mode="python")
    values.update(
        requested_inputs_json=values.pop("requested_inputs"),
        source_references_json=values.pop("source_references"),
        configuration_references_json=values.pop("configuration_references"),
        code_references_json=values.pop("code_references"),
    )
    values["contract_version"] = PIPELINE_RUN_CONTRACT_VERSION
    values.pop("execution_allowed")
    values["id"] = str(uuid4())
    with database.session() as session:
        existing = session.scalar(
            select(ResearchPipelineRunRecord).where(
                ResearchPipelineRunRecord.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if not _same_idempotent_request(existing, fingerprint):
                raise IdempotencyConflict("idempotency key is already bound to a different request")
            return existing
        row = ResearchPipelineRunRecord(**values)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(ResearchPipelineRunRecord).where(
                    ResearchPipelineRunRecord.idempotency_key == idempotency_key
                )
            )
            if existing is None:
                raise ResearchPipelineError("pipeline creation lost its uniqueness race") from None
            if not _same_idempotent_request(existing, fingerprint):
                raise IdempotencyConflict(
                    "idempotency key is already bound to a different request"
                ) from None
            return existing
        return row


def _stage_record(session, stage_run_id: str) -> ResearchStageRunRecord:
    row = session.scalar(
        select(ResearchStageRunRecord).where(ResearchStageRunRecord.stage_run_id == stage_run_id)
    )
    if row is None:
        raise ResearchPipelineError(f"stage attempt not found: {stage_run_id}")
    return row


def _validate_transition(current: ResearchStatus, target: ResearchStatus) -> None:
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalResearchTransition(f"{current} -> {target} is not legal")


def _transition_values(model, target: ResearchStatus, *, reason: str | None, now: datetime):
    values = {"status": target, "terminal_reason": reason}
    if target == "RUNNING":
        values["started_at"] = now
    if target in {"PASS", "FAILED", "BLOCKED", "SKIPPED", "CANCELLED"}:
        values["completed_at"] = now
    if hasattr(model, "blocked_reason_code") and target == "BLOCKED":
        values["blocked_reason_code"] = reason
    if hasattr(model, "failure_reason_code") and target == "FAILED":
        values["failure_reason_code"] = reason
    return values


def _transition_record(
    session,
    model,
    identity_filter,
    *,
    expected: ResearchStatus,
    target: ResearchStatus,
    reason: str | None,
    now: datetime,
):
    """Apply one status transition with the database as the concurrency authority."""

    _validate_transition(expected, target)
    result = session.execute(
        update(model)
        .where(identity_filter, model.status == expected)
        .values(**_transition_values(model, target, reason=reason, now=now))
    )
    if result.rowcount != 1:
        raise TransitionConflict("transition conflict: persisted status changed")
    return session.scalar(select(model).where(identity_filter))


def transition_stage(
    database, stage_run_id: str, target: ResearchStatus, *, reason: str | None = None
):
    with database.session() as session:
        row = _stage_record(session, stage_run_id)
        return _transition_record(
            session,
            ResearchStageRunRecord,
            ResearchStageRunRecord.stage_run_id == stage_run_id,
            expected=row.status,
            target=target,
            reason=reason,
            now=_utc_now(),
        )


def _transition_stage_expected(
    database,
    stage_run_id: str,
    expected: ResearchStatus,
    target: ResearchStatus,
    *,
    reason: str | None = None,
):
    """Testable CAS boundary for callers that already validated a stage state."""

    with database.session() as session:
        return _transition_record(
            session,
            ResearchStageRunRecord,
            ResearchStageRunRecord.stage_run_id == stage_run_id,
            expected=expected,
            target=target,
            reason=reason,
            now=_utc_now(),
        )


def _pipeline_record(session, run_id: str) -> ResearchPipelineRunRecord:
    row = session.scalar(
        select(ResearchPipelineRunRecord).where(ResearchPipelineRunRecord.run_id == run_id)
    )
    if row is None:
        raise ResearchPipelineError(f"pipeline run not found: {run_id}")
    return row


def transition_pipeline(
    database, run_id: str, target: ResearchStatus, *, reason: str | None = None
):
    with database.session() as session:
        row = _pipeline_record(session, run_id)
        return _transition_record(
            session,
            ResearchPipelineRunRecord,
            ResearchPipelineRunRecord.run_id == run_id,
            expected=row.status,
            target=target,
            reason=reason,
            now=_utc_now(),
        )


def _transition_pipeline_expected(
    database,
    run_id: str,
    expected: ResearchStatus,
    target: ResearchStatus,
    *,
    reason: str | None = None,
):
    """Testable CAS boundary for callers that already validated a pipeline state."""

    with database.session() as session:
        return _transition_record(
            session,
            ResearchPipelineRunRecord,
            ResearchPipelineRunRecord.run_id == run_id,
            expected=expected,
            target=target,
            reason=reason,
            now=_utc_now(),
        )


def create_stage_attempt(
    database,
    *,
    pipeline_run_id: str,
    stage_key: str,
    stage_version: str,
    status: ResearchStatus = "READY",
    attempt_number: int | None = None,
    input_references: tuple[EmbeddedReferenceV1, ...] = (),
    evidence_references: tuple[EmbeddedReferenceV1, ...] = (),
) -> ResearchStageRunRecord:
    if status not in {"WAITING", "READY"}:
        raise ValueError("new stage attempts must begin in WAITING or READY")
    with database.session() as session:
        pipeline = session.scalar(
            select(ResearchPipelineRunRecord).where(
                ResearchPipelineRunRecord.run_id == pipeline_run_id
            )
        )
        if pipeline is None:
            raise ResearchPipelineError(f"pipeline run not found: {pipeline_run_id}")
        if attempt_number is None:
            latest = session.scalar(
                select(func.max(ResearchStageRunRecord.attempt_number)).where(
                    ResearchStageRunRecord.pipeline_run_id == pipeline.id,
                    ResearchStageRunRecord.stage_key == stage_key,
                )
            )
            attempt_number = int(latest or 0) + 1
        now = _utc_now()
        values = ResearchStageRun(
            stage_run_id=str(uuid4()),
            pipeline_run_id=pipeline.run_id,
            stage_key=stage_key,
            stage_version=stage_version,
            attempt_number=attempt_number,
            status=status,
            input_references=input_references,
            evidence_references=evidence_references,
            created_at=now,
        ).model_dump(mode="python")
        values.update(
            input_references_json=values.pop("input_references"),
            evidence_references_json=values.pop("evidence_references"),
            gate_evidence_json=values.pop("gate_evidence"),
            lineage_references_json=values.pop("lineage_references"),
            pipeline_run_id=pipeline.id,
        )
        values["contract_version"] = STAGE_RUN_CONTRACT_VERSION
        values.pop("execution_allowed")
        values["id"] = str(uuid4())
        row = ResearchStageRunRecord(**values)
        session.add(row)
        session.flush()
        return row


def retry_stage_attempt(database, stage_run_id: str) -> ResearchStageRunRecord:
    with database.session() as session:
        previous = _stage_record(session, stage_run_id)
        if previous.status not in {"FAILED", "BLOCKED"}:
            raise IllegalResearchTransition("only FAILED or BLOCKED attempts can be retried")
        pipeline = session.get(ResearchPipelineRunRecord, previous.pipeline_run_id)
        assert pipeline is not None
        row = ResearchStageRunRecord(
            id=str(uuid4()),
            stage_run_id=str(uuid4()),
            pipeline_run_id=pipeline.id,
            contract_version=STAGE_RUN_CONTRACT_VERSION,
            stage_key=previous.stage_key,
            stage_version=previous.stage_version,
            attempt_number=previous.attempt_number + 1,
            status="READY",
            progress_processed=0,
            progress_total=None,
            progress_unit=None,
            input_references_json=list(previous.input_references_json or []),
            evidence_references_json=list(previous.evidence_references_json or []),
            gate_evidence_json=[],
            lineage_references_json=[],
            execution_allowed=False,
        )
        session.add(row)
        session.flush()
        return row


def update_stage_progress(
    database, stage_run_id: str, *, processed: int, total: int | None, unit: str | None
):
    if processed < 0 or (total is not None and (total < 0 or processed > total)):
        raise ValueError("invalid progress counters")
    if total is not None and not unit:
        raise ValueError("progress unit is required when total is known")
    with database.session() as session:
        row = _stage_record(session, stage_run_id)
        if row.status not in {"READY", "RUNNING"}:
            raise IllegalResearchTransition(
                "progress can only change before a terminal stage state"
            )
        row.progress_processed = processed
        row.progress_total = total
        row.progress_unit = unit
        return row


def register_artifact(
    database,
    *,
    pipeline_run_id: str,
    producer_stage_run_id: str,
    artifact_id: str,
    artifact_kind: str,
    content_sha256: str,
    artifact_format: str,
    size_bytes: int,
    logical_locator: str,
    manifest_sha256: str | None = None,
    gate_evidence: tuple[QualityGateEvidenceV1, ...] = (),
    lineage_references: tuple[EmbeddedReferenceV1, ...] = (),
) -> ResearchArtifactRecord:
    now = _utc_now()
    contract = ResearchArtifact(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        content_sha256=content_sha256,
        manifest_sha256=manifest_sha256,
        artifact_format=artifact_format,
        size_bytes=size_bytes,
        logical_locator=logical_locator,
        pipeline_run_id=pipeline_run_id,
        producer_stage_run_id=producer_stage_run_id,
        gate_evidence=gate_evidence,
        lineage_references=lineage_references,
        created_at=now,
    )
    with database.session() as session:
        pipeline = session.scalar(
            select(ResearchPipelineRunRecord).where(
                ResearchPipelineRunRecord.run_id == pipeline_run_id
            )
        )
        stage = session.scalar(
            select(ResearchStageRunRecord).where(
                ResearchStageRunRecord.stage_run_id == producer_stage_run_id
            )
        )
        if pipeline is None or stage is None or stage.pipeline_run_id != pipeline.id:
            raise ResearchPipelineError(
                "artifact producer pipeline/stage relationship is invalid"
            )
        existing = session.scalar(
            select(ResearchArtifactRecord).where(ResearchArtifactRecord.artifact_id == artifact_id)
        )
        if existing is not None:
            if existing.content_sha256 != content_sha256:
                raise ArtifactIdentityConflict("artifact_id is already bound to different content")
            return existing
        content_existing = session.scalar(
            select(ResearchArtifactRecord).where(
                ResearchArtifactRecord.content_sha256 == content_sha256
            )
        )
        if content_existing is not None:
            raise ArtifactContentConflict(
                "content_sha256 is already registered under another artifact identity"
            )
        values = contract.model_dump(mode="python")
        values.update(
            pipeline_run_id=pipeline.id,
            producer_stage_run_id=stage.id,
            gate_evidence_json=values.pop("gate_evidence"),
            lineage_references_json=values.pop("lineage_references"),
        )
        values["contract_version"] = ARTIFACT_CONTRACT_VERSION
        values.pop("execution_allowed")
        values["id"] = str(uuid4())
        row = ResearchArtifactRecord(**values)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            raise ArtifactIdentityConflict(
                "artifact registration lost a database uniqueness race"
            ) from None
        return row
