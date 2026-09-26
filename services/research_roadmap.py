"""Read-only Research Roadmap projections over persisted research metadata."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from models.research_roadmap import (
    RoadmapArtifactV1,
    RoadmapDatasetSummaryV1,
    RoadmapEventV1,
    RoadmapResponseV1,
    RoadmapRunHistoryV1,
    RoadmapRunV1,
    RoadmapSourceV1,
    RoadmapStageV1,
    RoadmapTimelineV1,
)
from persistence.orm import (
    ResearchArtifactRecord,
    ResearchPipelineRunRecord,
    ResearchStageRunRecord,
)

_TERMINAL_STATES = frozenset({"PASS", "FAILED", "BLOCKED", "CANCELLED", "SKIPPED"})
_ACTIVE_STATES = frozenset({"WAITING", "READY", "RUNNING"})
_SAFE_LOGICAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SAFE_REASON = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,99}$")
_MAX_TIMELINE_EVENTS = 100
_MAX_STAGE_ROWS = 32
_MAX_ARTIFACT_ROWS = 16
_DEFAULT_HISTORY_LIMIT = 20
_DEFAULT_TIMELINE_LIMIT = 100
_PREVIEW_LIMIT = 10
_EVENT_ORDER = {
    "PIPELINE_CREATED": 10,
    "PIPELINE_STARTED": 20,
    "RETRY_CREATED": 30,
    "STAGE_STARTED": 40,
    "STAGE_PROGRESS": 50,
    "DATASET_BUILT": 60,
    "DATASET_AUDIT_PASSED": 70,
    "DATASET_AUDIT_FAILED": 71,
    "ARTIFACT_REGISTERED": 80,
    "ARTIFACT_VALIDATED": 90,
    "ARTIFACT_PUBLISHED": 100,
    "STAGE_PASSED": 110,
    "STAGE_FAILED": 111,
    "STAGE_BLOCKED": 112,
    "STAGE_CANCELLED": 113,
    "PIPELINE_PASSED": 120,
    "PIPELINE_FAILED": 121,
    "PIPELINE_BLOCKED": 122,
    "PIPELINE_CANCELLED": 123,
}


class RoadmapError(RuntimeError):
    """Sanitized, client-safe roadmap failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RoadmapRunNotFound(RoadmapError):
    def __init__(self) -> None:
        super().__init__("ROADMAP_RUN_NOT_FOUND")


class RoadmapInvalidRequest(RoadmapError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


@dataclass(frozen=True)
class _TimelineCandidate:
    event_type: str
    timestamp: datetime
    source_identity: str
    stage_key: str | None = None
    attempt: int | None = None
    message: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def sort_key(self) -> tuple[datetime, int, str, str, int]:
        return (
            self.timestamp,
            _EVENT_ORDER.get(self.event_type, 1000),
            self.event_type,
            self.source_identity,
            self.attempt or 0,
        )


def _safe_logical(value: object, *, maximum: int = 200) -> str | None:
    if not isinstance(value, str) or len(value) > maximum or not _SAFE_LOGICAL.fullmatch(value):
        return None
    return value


def _safe_reason(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_REASON.fullmatch(value):
        return None
    return value


def _safe_identity(value: object, fallback: str) -> str:
    return _safe_logical(value) or fallback


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _validate_limit(value: object, *, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RoadmapInvalidRequest("ROADMAP_INVALID_LIMIT") from None
    if parsed < 1 or parsed > maximum:
        raise RoadmapInvalidRequest("ROADMAP_INVALID_LIMIT")
    return parsed


def _run_order(row: ResearchPipelineRunRecord) -> tuple[datetime, str]:
    return (_aware(row.created_at), row.run_id)


def _choose_current_run(rows: list[ResearchPipelineRunRecord]) -> ResearchPipelineRunRecord | None:
    active = [row for row in rows if row.status not in _TERMINAL_STATES]
    candidates = active or [row for row in rows if row.status in _TERMINAL_STATES]
    return max(candidates, key=_run_order) if candidates else None


def _safe_reference_map(references: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(references, list):
        return result
    allowed = {
        "dataset_contract",
        "feature_contract",
        "label_contract",
        "builder_semantics",
        "source_population",
    }
    for item in references:
        if not isinstance(item, dict):
            continue
        reference_type = item.get("reference_type")
        reference_id = item.get("reference_id")
        if reference_type in allowed:
            safe_id = _safe_logical(reference_id, maximum=128)
            if safe_id is not None:
                result[str(reference_type)] = safe_id
    return result


def _source(run: ResearchPipelineRunRecord) -> RoadmapSourceV1:
    requested = run.requested_inputs_json if isinstance(run.requested_inputs_json, dict) else {}
    references = _safe_reference_map(run.source_references_json)
    source_id = _safe_logical(requested.get("source_logical_id"), maximum=128)
    if source_id is None:
        source_id = references.get("source_population")
    versions = {
        key: references[key]
        for key in (
            "dataset_contract",
            "feature_contract",
            "label_contract",
            "builder_semantics",
        )
        if key in references
    }
    return RoadmapSourceV1(
        source_type="LOGICAL_RESEARCH_SOURCE",
        source_logical_id=source_id,
        symbol_policy="EXACT_SYMBOL",
        causal_cutoff_policy="CANDIDATE_DETECTED_AT_UTC",
        continuity_policy="SESSION_AWARE_V2_ACCEPTED",
        contract_versions=versions,
    )


def _reason(stage: ResearchStageRunRecord) -> str | None:
    return next(
        (
            safe
            for value in (
                stage.blocked_reason_code,
                stage.failure_reason_code,
                stage.terminal_reason,
            )
            if (safe := _safe_reason(value)) is not None
        ),
        None,
    )


def _stage_views(
    stages: list[ResearchStageRunRecord],
) -> tuple[RoadmapStageV1, ...]:
    first_created: dict[str, datetime] = {}
    for stage in stages:
        first_created[stage.stage_key] = min(
            first_created.get(stage.stage_key, _aware(stage.created_at)),
            _aware(stage.created_at),
        )
    stages = sorted(
        stages,
        key=lambda item: (
            first_created[item.stage_key],
            item.stage_key,
            item.attempt_number,
            item.stage_run_id,
        ),
    )
    latest_attempt: dict[str, int] = {}
    for stage in stages:
        latest_attempt[stage.stage_key] = max(
            latest_attempt.get(stage.stage_key, 0), stage.attempt_number
        )
    result: list[RoadmapStageV1] = []
    for stage in stages:
        total = stage.progress_total
        processed = stage.progress_processed
        percentage = None
        progress_mode = "INDETERMINATE"
        if total is not None:
            progress_mode = "DETERMINATE"
            if total > 0:
                percentage = round(min(100.0, max(0.0, processed / total * 100.0)), 2)
        result.append(
            RoadmapStageV1(
                stage_key=_safe_logical(stage.stage_key, maximum=64) or "UNKNOWN_STAGE",
                attempt=stage.attempt_number,
                lifecycle_state=stage.status,
                processed=processed,
                total=total,
                unit=_safe_logical(stage.progress_unit, maximum=64),
                progress_mode=progress_mode,
                display_percentage=percentage,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
                reason=_reason(stage),
                is_current_attempt=stage.attempt_number == latest_attempt[stage.stage_key],
                retry_count=max(0, latest_attempt[stage.stage_key] - 1),
            )
        )
    return tuple(result)


def _current_stage(
    run: ResearchPipelineRunRecord,
    stages: list[ResearchStageRunRecord],
) -> str | None:
    if not stages:
        return None
    ordered = sorted(
        stages,
        key=lambda item: (_aware(item.created_at), item.stage_key, item.attempt_number),
    )
    latest: dict[str, ResearchStageRunRecord] = {}
    for stage in ordered:
        latest[stage.stage_key] = stage
    logical = list(latest.values())
    active = [stage for stage in logical if stage.status in _ACTIVE_STATES]
    if active:
        return min(active, key=lambda item: (_aware(item.created_at), item.stage_key)).stage_key
    incomplete = [stage for stage in logical if stage.status not in {"PASS", "SKIPPED"}]
    if incomplete:
        return min(incomplete, key=lambda item: (_aware(item.created_at), item.stage_key)).stage_key
    return max(
        logical,
        key=lambda item: (_aware(item.completed_at or item.created_at), item.stage_key),
    ).stage_key


def _run_view(run: ResearchPipelineRunRecord, stages: list[ResearchStageRunRecord]) -> RoadmapRunV1:
    return RoadmapRunV1(
        run_id=_safe_logical(run.run_id, maximum=100) or "UNKNOWN_RUN",
        pipeline_key=_safe_logical(run.experiment_key) or "UNKNOWN_PIPELINE",
        pipeline_contract_version=_safe_logical(run.pipeline_version, maximum=64)
        or "UNKNOWN_CONTRACT",
        lifecycle_state=_safe_reason(run.status) or "UNKNOWN",
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        current_stage_key=_current_stage(run, stages),
        execution_allowed=False,
    )


def _summary_from_stage(stages: list[ResearchStageRunRecord]) -> RoadmapDatasetSummaryV1 | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for stage in stages:
        if stage.stage_key != "DATASET_BUILD" or stage.status != "PASS":
            continue
        for evidence in stage.gate_evidence_json or []:
            if (
                evidence.get("gate_id") == "dataset_build_summary"
                and evidence.get("status") == "PASS"
            ):
                measured = evidence.get("measured")
                if isinstance(measured, dict):
                    candidates.append((_aware(stage.completed_at or stage.created_at), measured))
    if not candidates:
        return None
    measured = max(candidates, key=lambda item: item[0])[1]
    try:
        versions = _safe_reference_map(
            [
                {
                    "reference_type": "dataset_contract",
                    "reference_id": measured["dataset_contract_version"],
                },
                {
                    "reference_type": "feature_contract",
                    "reference_id": measured["feature_contract_version"],
                },
                {
                    "reference_type": "label_contract",
                    "reference_id": measured.get(
                        "label_contract_version", "forward_outcome_labels_v1"
                    ),
                },
            ]
        )
        return RoadmapDatasetSummaryV1(
            candidates_inspected=int(measured["candidates_inspected"]),
            rows_produced=int(measured["rows_produced"]),
            trainable_rows=int(measured["trainable_rows"]),
            non_trainable_rows=int(measured["non_trainable_rows"]),
            unresolved_rows=int(measured["unresolved_rows"]),
            excluded_rows=int(measured["excluded_rows"]),
            semantic_dataset_hash=str(measured["dataset_semantic_hash"]),
            dataset_contract_version=versions.get("dataset_contract", "unknown"),
            feature_contract_version=versions.get("feature_contract", "unknown"),
            label_contract_version=versions.get("label_contract", "unknown"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _artifact_views(
    artifacts: list[ResearchArtifactRecord],
    stages_by_id: dict[str, ResearchStageRunRecord],
) -> tuple[RoadmapArtifactV1, ...]:
    result: list[RoadmapArtifactV1] = []
    for artifact in artifacts:
        stage = stages_by_id.get(artifact.producer_stage_run_id)
        if stage is None:
            continue
        semantic_hash = None
        for reference in artifact.lineage_references_json or []:
            if (
                isinstance(reference, dict)
                and reference.get("reference_type") == "dataset_semantic_hash"
            ):
                semantic_hash = reference.get("reference_id")
                break
        if not isinstance(semantic_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", semantic_hash):
            semantic_hash = None
        result.append(
            RoadmapArtifactV1(
                artifact_id=_safe_logical(artifact.artifact_id, maximum=100)
                or "UNKNOWN_ARTIFACT",
                artifact_type=_safe_logical(artifact.artifact_kind, maximum=64)
                or "UNKNOWN_ARTIFACT",
                logical_identity=_safe_logical(artifact.logical_locator) or artifact.artifact_kind,
                validation_state=artifact.validation_status,
                publication_state=artifact.publication_status,
                content_sha256=artifact.content_sha256,
                semantic_dataset_hash=semantic_hash,
                producer_stage=_safe_logical(stage.stage_key, maximum=64)
                or "UNKNOWN_STAGE",
                producer_attempt=stage.attempt_number,
                created_at=artifact.created_at,
                published_at=artifact.published_at,
            )
        )
    return tuple(result)


def _metadata_for_gate(gate: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "candidates_inspected",
        "rows_produced",
        "trainable_rows",
        "non_trainable_rows",
        "unresolved_rows",
        "excluded_rows",
        "verified_artifacts",
    }
    return {key: gate["measured"][key] for key in allowed if key in gate.get("measured", {})}


def _timeline_candidates(
    run: ResearchPipelineRunRecord,
    stages: list[ResearchStageRunRecord],
    artifacts: list[ResearchArtifactRecord],
) -> list[_TimelineCandidate]:
    events: list[_TimelineCandidate] = [
        _TimelineCandidate(
            "PIPELINE_CREATED",
            _aware(run.created_at),
            f"pipeline:{_safe_identity(run.run_id, 'unknown')}",
            message="Pipeline created",
        )
    ]
    if run.started_at is not None:
        events.append(
            _TimelineCandidate(
                "PIPELINE_STARTED",
                _aware(run.started_at),
                f"pipeline-start:{_safe_identity(run.run_id, 'unknown')}",
                message="Pipeline started",
            )
        )
    terminal_event = {
        "PASS": "PIPELINE_PASSED",
        "FAILED": "PIPELINE_FAILED",
        "BLOCKED": "PIPELINE_BLOCKED",
        "CANCELLED": "PIPELINE_CANCELLED",
    }.get(run.status)
    if terminal_event and run.completed_at is not None:
        events.append(
            _TimelineCandidate(
                terminal_event,
                _aware(run.completed_at),
                f"pipeline-terminal:{run.run_id}",
                message=f"Pipeline {run.status.lower()}",
            )
        )
    status_events = {
        "PASS": "STAGE_PASSED",
        "FAILED": "STAGE_FAILED",
        "BLOCKED": "STAGE_BLOCKED",
        "CANCELLED": "STAGE_CANCELLED",
    }
    for stage in stages:
        stage_identity = _safe_identity(
            stage.stage_run_id, f"{stage.stage_key}:{stage.attempt_number}"
        )
        if stage.attempt_number > 1:
            events.append(
                _TimelineCandidate(
                    "RETRY_CREATED",
                    _aware(stage.created_at),
                    f"retry:{stage_identity}",
                    stage.stage_key,
                    stage.attempt_number,
                    message=f"Retry attempt {stage.attempt_number} created",
                    metadata={"retry_count": stage.attempt_number - 1},
                )
            )
        if stage.started_at is not None:
            events.append(
                _TimelineCandidate(
                    "STAGE_STARTED",
                    _aware(stage.started_at),
                    f"start:{stage_identity}",
                    stage.stage_key,
                    stage.attempt_number,
                    message="Stage started",
                )
            )
        if stage.progress_total is not None or stage.progress_processed > 0:
            events.append(
                _TimelineCandidate(
                    "STAGE_PROGRESS",
                    _aware(stage.completed_at or stage.started_at or stage.created_at),
                    f"progress:{stage_identity}",
                    stage.stage_key,
                    stage.attempt_number,
                    message="Current persisted progress",
                    metadata={
                        "processed": stage.progress_processed,
                        "total": stage.progress_total,
                        "unit": stage.progress_unit,
                    },
                )
            )
        if stage.completed_at is not None and stage.status in status_events:
            events.append(
                _TimelineCandidate(
                    status_events[stage.status],
                    _aware(stage.completed_at),
                    f"status:{stage_identity}",
                    stage.stage_key,
                    stage.attempt_number,
                    message=f"Stage {stage.status.lower()}",
                )
            )
        for gate in stage.gate_evidence_json or []:
            gate_id = gate.get("gate_id")
            gate_status = gate.get("status")
            if gate_id == "dataset_build_summary" and gate_status == "PASS":
                events.append(
                    _TimelineCandidate(
                        "DATASET_BUILT",
                        _aware(stage.completed_at or stage.created_at),
                        f"dataset-built:{stage_identity}",
                        stage.stage_key,
                        stage.attempt_number,
                        message="Dataset build completed",
                        metadata=_metadata_for_gate(gate),
                    )
                )
            if gate_id == "dataset_contract_audit":
                audit_type = (
                    "DATASET_AUDIT_PASSED"
                    if gate_status == "PASS"
                    else "DATASET_AUDIT_FAILED"
                )
                events.append(
                    _TimelineCandidate(
                        audit_type,
                        _aware(stage.completed_at or stage.created_at),
                        f"audit:{stage_identity}",
                        stage.stage_key,
                        stage.attempt_number,
                        message="Dataset audit result recorded",
                        metadata=_metadata_for_gate(gate),
                    )
                )
    stages_by_id = {stage.id: stage for stage in stages}
    for artifact in artifacts:
        stage = stages_by_id.get(artifact.producer_stage_run_id)
        if stage is None:
            continue
        artifact_identity = (
            f"artifact:{_safe_identity(artifact.artifact_id, artifact.artifact_kind)}"
        )
        events.append(
            _TimelineCandidate(
                "ARTIFACT_REGISTERED",
                _aware(artifact.created_at),
                artifact_identity,
                stage.stage_key,
                stage.attempt_number,
                message="Artifact metadata registered",
            )
        )
        if artifact.validation_status == "VALID" and artifact.published_at is not None:
            events.append(
                _TimelineCandidate(
                    "ARTIFACT_VALIDATED",
                    _aware(artifact.published_at),
                    f"validated:{_safe_identity(artifact.artifact_id, artifact.artifact_kind)}",
                    stage.stage_key,
                    stage.attempt_number,
                    message="Artifact validated",
                )
            )
        if artifact.publication_status == "PUBLISHED" and artifact.published_at is not None:
            events.append(
                _TimelineCandidate(
                    "ARTIFACT_PUBLISHED",
                    _aware(artifact.published_at),
                    f"published:{_safe_identity(artifact.artifact_id, artifact.artifact_kind)}",
                    stage.stage_key,
                    stage.attempt_number,
                    message="Artifact published",
                )
            )
    return sorted(events, key=lambda item: item.sort_key)


def _event_view(event: _TimelineCandidate) -> RoadmapEventV1:
    return RoadmapEventV1(
        event_type=event.event_type,
        timestamp=event.timestamp,
        stage_key=_safe_logical(event.stage_key, maximum=64),
        attempt=event.attempt,
        message=event.message,
        metadata=event.metadata or {},
    )


def _encode_cursor(event: _TimelineCandidate, run_id: str) -> str:
    payload = {
        "run_id": _safe_identity(run_id, "unknown"),
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type,
        "source_identity": event.source_identity,
        "attempt": event.attempt or 0,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None, *, expected_run_id: str
) -> tuple[datetime, int, str, str, int] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
        raise RoadmapInvalidRequest("ROADMAP_INVALID_CURSOR")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        cursor_run_id = value["run_id"]
        timestamp = datetime.fromisoformat(value["timestamp"])
        event_type = value["event_type"]
        source_identity = value["source_identity"]
        attempt = value["attempt"]
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError, binascii.Error):
        raise RoadmapInvalidRequest("ROADMAP_INVALID_CURSOR") from None
    if (
        timestamp.tzinfo is None
        or cursor_run_id != _safe_identity(expected_run_id, "unknown")
        or not isinstance(event_type, str)
        or event_type not in _EVENT_ORDER
        or not isinstance(source_identity, str)
        or not _SAFE_LOGICAL.fullmatch(source_identity)
        or not isinstance(attempt, int)
        or attempt < 0
    ):
        raise RoadmapInvalidRequest("ROADMAP_INVALID_CURSOR")
    return (_aware(timestamp), _EVENT_ORDER[event_type], event_type, source_identity, attempt)


def _load_run_details(database, run: ResearchPipelineRunRecord):
    with database.session() as session:
        stages = list(
            session.scalars(
                select(ResearchStageRunRecord)
                .where(ResearchStageRunRecord.pipeline_run_id == run.id)
                .order_by(
                    desc(ResearchStageRunRecord.created_at),
                    desc(ResearchStageRunRecord.attempt_number),
                    desc(ResearchStageRunRecord.stage_run_id),
                )
                .limit(_MAX_STAGE_ROWS)
            ).all()
        )
        artifacts = list(
            session.scalars(
                select(ResearchArtifactRecord)
                .where(ResearchArtifactRecord.pipeline_run_id == run.id)
                .order_by(
                    desc(ResearchArtifactRecord.created_at),
                    desc(ResearchArtifactRecord.artifact_id),
                )
                .limit(_MAX_ARTIFACT_ROWS)
            ).all()
        )
    return stages, artifacts


def _build_response(
    run: ResearchPipelineRunRecord | None,
    stages: list[ResearchStageRunRecord],
    artifacts: list[ResearchArtifactRecord],
    history: list[ResearchPipelineRunRecord],
) -> RoadmapResponseV1:
    if run is None:
        return RoadmapResponseV1(recent_runs=tuple(_history_view(item) for item in history))
    stage_views = _stage_views(stages)
    candidates = _timeline_candidates(run, stages, artifacts)
    return RoadmapResponseV1(
        current_run=_run_view(run, stages),
        stages=stage_views,
        dataset_summary=_summary_from_stage(stages),
        artifacts=_artifact_views(artifacts, {stage.id: stage for stage in stages}),
        timeline_preview=tuple(_event_view(item) for item in candidates[-_PREVIEW_LIMIT:]),
        source=_source(run),
        recent_runs=tuple(_history_view(item) for item in history),
    )


def _history_view(run: ResearchPipelineRunRecord) -> RoadmapRunHistoryV1:
    return RoadmapRunHistoryV1(
        run_id=_safe_logical(run.run_id, maximum=100) or "UNKNOWN_RUN",
        pipeline_key=_safe_logical(run.experiment_key) or "UNKNOWN_PIPELINE",
        lifecycle_state=_safe_reason(run.status) or "UNKNOWN",
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def get_roadmap_overview(database, *, limit: object = _DEFAULT_HISTORY_LIMIT) -> RoadmapResponseV1:
    history_limit = _validate_limit(limit, maximum=200)
    try:
        with database.session() as session:
            history = list(
                session.scalars(
                    select(ResearchPipelineRunRecord)
                    .order_by(
                        desc(ResearchPipelineRunRecord.created_at),
                        desc(ResearchPipelineRunRecord.run_id),
                    )
                    .limit(history_limit)
                ).all()
            )
            active = list(
                session.scalars(
                    select(ResearchPipelineRunRecord)
                    .where(ResearchPipelineRunRecord.status.in_(tuple(_ACTIVE_STATES)))
                    .order_by(
                        desc(ResearchPipelineRunRecord.created_at),
                        desc(ResearchPipelineRunRecord.run_id),
                    )
                    .limit(1)
                ).all()
            )
            current = active[0] if active else _choose_current_run(history)
        stages, artifacts = (
            _load_run_details(database, current) if current is not None else ([], [])
        )
        return _build_response(current, stages, artifacts, history)
    except RoadmapError:
        raise
    except Exception:
        raise RoadmapError("ROADMAP_QUERY_FAILED") from None


def get_roadmap_run(database, run_id: str) -> RoadmapResponseV1:
    try:
        with database.session() as session:
            run = session.scalar(
                select(ResearchPipelineRunRecord).where(ResearchPipelineRunRecord.run_id == run_id)
            )
        if run is None:
            raise RoadmapRunNotFound()
        stages, artifacts = _load_run_details(database, run)
        return _build_response(run, stages, artifacts, [run])
    except RoadmapError:
        raise
    except Exception:
        raise RoadmapError("ROADMAP_QUERY_FAILED") from None


def get_roadmap_timeline(
    database,
    run_id: str,
    *,
    limit: object = _DEFAULT_TIMELINE_LIMIT,
    cursor: str | None = None,
) -> RoadmapTimelineV1:
    page_limit = _validate_limit(limit, maximum=_MAX_TIMELINE_EVENTS)
    try:
        with database.session() as session:
            run = session.scalar(
                select(ResearchPipelineRunRecord).where(ResearchPipelineRunRecord.run_id == run_id)
            )
        if run is None:
            raise RoadmapRunNotFound()
        decoded = _decode_cursor(cursor, expected_run_id=run.run_id)
        stages, artifacts = _load_run_details(database, run)
        candidates = _timeline_candidates(run, stages, artifacts)
        if decoded is not None:
            candidates = [item for item in candidates if item.sort_key > decoded]
        page = candidates[:page_limit]
        next_cursor = (
            _encode_cursor(page[-1], run.run_id)
            if len(candidates) > len(page) and page
            else None
        )
        return RoadmapTimelineV1(
            run_id=_safe_logical(run.run_id, maximum=100) or "UNKNOWN_RUN",
            events=tuple(_event_view(item) for item in page),
            next_cursor=next_cursor,
        )
    except RoadmapError:
        raise
    except Exception:
        raise RoadmapError("ROADMAP_QUERY_FAILED") from None


__all__ = [
    "RoadmapError",
    "RoadmapInvalidRequest",
    "RoadmapRunNotFound",
    "get_roadmap_overview",
    "get_roadmap_run",
    "get_roadmap_timeline",
]
