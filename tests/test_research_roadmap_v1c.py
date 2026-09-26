from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from api.app import create_app
from config.remote_read_only_policy import is_remote_path_allowed
from config.settings import Settings
from models.model_inference_dataset import DatasetRowV1
from persistence.database import Database
from persistence.orm import (
    ResearchArtifactRecord,
    ResearchPipelineRunRecord,
    ResearchStageRunRecord,
)
from services.model_inference_dataset import _stable
from services.research_dataset_pipeline import (
    retry_model_inference_dataset_pipeline,
    run_model_inference_dataset_pipeline,
)
from services.research_pipeline import create_pipeline_run, transition_pipeline
from services.research_roadmap import RoadmapError, get_roadmap_overview, get_roadmap_run


class _Builder:
    page_size = 2

    def __init__(self, rows=(), *, failure: bool = False, session_aware: bool = True):
        self.rows = tuple(rows)
        self.failure = failure
        self.session_aware_v2_accepted = session_aware

    def iter_rows(self):
        if self.failure:
            raise RuntimeError("fixture failure")
        yield from self.rows


def _database(tmp_path: Path) -> Database:
    database = Database.for_test(f"sqlite:///{(tmp_path / 'roadmap.db').as_posix()}")
    database.create_test_schema()
    return database


def _row(candidate_id: str) -> DatasetRowV1:
    timestamp = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    candidate = DatasetRowV1(
        candidate_id=candidate_id,
        symbol="XAUUSD",
        candidate_timestamp=timestamp,
        causal_cutoff_timestamp=timestamp,
        state="PRE_SIGNAL_OBSERVATION",
        training_eligibility="NON_TRAINABLE",
        reason_codes=("PRE_SIGNAL",),
        classification=None,
        outcome_label="NOT_ELIGIBLE",
        features={},
        source_provenance={"candidate_id": candidate_id},
        source_timestamps={},
        row_identity=hashlib.sha256(candidate_id.encode()).hexdigest(),
        row_fingerprint="0" * 64,
    )
    payload = candidate.model_dump(mode="json")
    payload.pop("row_fingerprint")
    return candidate.model_copy(
        update={
            "row_fingerprint": hashlib.sha256(_stable(payload).encode()).hexdigest()
        }
    )


def _run(database, tmp_path: Path, key: str, *, builder=None, total=None):
    return run_model_inference_dataset_pipeline(
        database,
        idempotency_key=key,
        output_dir=tmp_path / "artifacts",
        builder=builder,
        total_candidates=total,
    )


def _client(database, tmp_path: Path, *, remote: bool = False) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'roadmap.db').as_posix()}",
        remote_dashboard_mode=remote,
    )
    return TestClient(create_app(settings=settings, database=database))


def test_empty_overview_is_explicit_and_read_only(tmp_path):
    database = _database(tmp_path)
    try:
        response = _client(database, tmp_path).get("/api/research/roadmap")
        assert response.status_code == 200
        payload = response.json()
        assert payload["contract_version"] == "research_roadmap_api_v1"
        assert payload["current_run"] is None
        assert payload["stages"] == []
        assert payload["artifacts"] == []
    finally:
        database.dispose()


def test_normal_pipeline_overview_exposes_bounded_summary_source_and_artifacts(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, "normal", builder=_Builder((_row("one"),)), total=1)
        response = _client(database, tmp_path).get("/api/research/roadmap")
        assert response.status_code == 200
        payload = response.json()
        assert payload["current_run"]["run_id"] == view.run_id
        assert payload["current_run"]["lifecycle_state"] == "PASS"
        assert payload["current_run"]["execution_allowed"] is False
        assert [stage["stage_key"] for stage in payload["stages"]] == [
            "DATA_SOURCE",
            "DATASET_BUILD",
            "DATASET_AUDIT",
            "ARTIFACT_VERIFY",
        ]
        build = next(stage for stage in payload["stages"] if stage["stage_key"] == "DATASET_BUILD")
        assert (build["processed"], build["total"], build["progress_mode"]) == (1, 1, "DETERMINATE")
        assert build["display_percentage"] == 100.0
        assert payload["dataset_summary"]["rows_produced"] == 1
        assert "accuracy" not in response.text.lower()
        assert payload["source"]["source_logical_id"] == "strategy_intelligence_forward_evidence_v1"
        assert len(payload["artifacts"]) == 2
        assert all("path" not in artifact for artifact in payload["artifacts"])
        assert str(tmp_path) not in response.text
        assert payload["timeline_preview"]
    finally:
        database.dispose()


def test_progress_modes_unknown_and_zero_do_not_fabricate_percentage(tmp_path):
    database = _database(tmp_path)
    try:
        unknown = _run(database, tmp_path, "unknown", builder=_Builder())
        zero = _run(database, tmp_path, "zero", builder=_Builder(), total=0)
        unknown_payload = get_roadmap_run(database, unknown.run_id).model_dump(mode="json")
        zero_payload = get_roadmap_run(database, zero.run_id).model_dump(mode="json")
        unknown_stage = unknown_payload["stages"][1]
        zero_stage = zero_payload["stages"][1]
        assert unknown_stage["progress_mode"] == "INDETERMINATE"
        assert unknown_stage["display_percentage"] is None
        assert zero_stage["progress_mode"] == "DETERMINATE"
        assert zero_stage["total"] == 0
        assert zero_stage["display_percentage"] is None
    finally:
        database.dispose()


def test_retry_history_marks_only_attempt_two_current(tmp_path):
    database = _database(tmp_path)
    try:
        failed = _run(database, tmp_path, "retry", builder=_Builder(failure=True))
        retry_model_inference_dataset_pipeline(
            database,
            run_id=failed.run_id,
            output_dir=tmp_path / "artifacts",
            builder=_Builder((_row("recovered"),)),
        )
        payload = get_roadmap_run(database, failed.run_id).model_dump(mode="json")
        build_attempts = [
            stage for stage in payload["stages"] if stage["stage_key"] == "DATASET_BUILD"
        ]
        assert [
            (stage["attempt"], stage["lifecycle_state"], stage["is_current_attempt"])
            for stage in build_attempts
        ] == [
            (1, "FAILED", False),
            (2, "PASS", True),
        ]
        assert all(stage["retry_count"] == 1 for stage in build_attempts)
    finally:
        database.dispose()


def test_current_run_prefers_newest_active_then_terminal_tie_is_deterministic(tmp_path):
    database = _database(tmp_path)
    try:
        terminal = create_pipeline_run(
            database,
            pipeline_version="terminal_pipeline_v1",
            experiment_key="terminal",
            idempotency_key="terminal",
        )
        transition_pipeline(database, terminal.run_id, "CANCELLED", reason="test")
        active = create_pipeline_run(
            database,
            pipeline_version="active_pipeline_v1",
            experiment_key="active",
            idempotency_key="active",
        )
        overview = get_roadmap_overview(database)
        assert overview.current_run is not None
        assert overview.current_run.run_id == active.run_id
        transition_pipeline(database, active.run_id, "CANCELLED", reason="test")

        first = create_pipeline_run(
            database,
            pipeline_version="tie_pipeline_v1",
            experiment_key="tie-a",
            idempotency_key="tie-a",
        )
        second = create_pipeline_run(
            database,
            pipeline_version="tie_pipeline_v1",
            experiment_key="tie-b",
            idempotency_key="tie-b",
        )
        with database.session() as session:
            rows = session.scalars(
                select(ResearchPipelineRunRecord).where(
                    ResearchPipelineRunRecord.run_id.in_((first.run_id, second.run_id))
                )
            ).all()
            fixed = datetime(2026, 1, 1, tzinfo=UTC)
            for row in rows:
                row.created_at = fixed
        # The active tie is resolved by created_at then run_id, never by DB order.
        overview = get_roadmap_overview(database)
        assert overview.current_run is not None
        assert overview.current_run.run_id == max(first.run_id, second.run_id)
    finally:
        database.dispose()


def test_timeline_is_truthful_deterministic_and_keyset_paginated(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, "timeline", builder=_Builder())
        client = _client(database, tmp_path)
        first = client.get(f"/api/research/roadmap/runs/{view.run_id}/timeline?limit=2")
        assert first.status_code == 200
        first_payload = first.json()
        assert len(first_payload["events"]) == 2
        assert first_payload["next_cursor"]
        second = client.get(
            f"/api/research/roadmap/runs/{view.run_id}/timeline?limit=2"
            f"&cursor={first_payload['next_cursor']}"
        )
        assert second.status_code == 200
        second_payload = second.json()
        assert second_payload["events"]
        assert first_payload["events"][-1] != second_payload["events"][0]
        other = _run(database, tmp_path, "timeline-other", builder=_Builder())
        cross_run = client.get(
            f"/api/research/roadmap/runs/{other.run_id}/timeline"
            f"?cursor={first_payload['next_cursor']}"
        )
        assert (cross_run.status_code, cross_run.json()["detail"]) == (
            400,
            "ROADMAP_INVALID_CURSOR",
        )
        again = client.get(f"/api/research/roadmap/runs/{view.run_id}/timeline?limit=100")
        assert again.json()["events"] == client.get(
            f"/api/research/roadmap/runs/{view.run_id}/timeline?limit=100"
        ).json()["events"]
        assert "1000 processed" not in again.text
    finally:
        database.dispose()


def test_api_errors_are_sanitized_and_routes_are_allowlisted(tmp_path):
    database = _database(tmp_path)
    try:
        client = _client(database, tmp_path)
        missing = client.get("/api/research/roadmap/runs/not-found")
        assert (missing.status_code, missing.json()["detail"]) == (404, "ROADMAP_RUN_NOT_FOUND")
        invalid_limit = client.get("/api/research/roadmap?limit=bad-secret")
        assert (invalid_limit.status_code, invalid_limit.json()["detail"]) == (
            400,
            "ROADMAP_INVALID_LIMIT",
        )
        view = _run(database, tmp_path, "invalid-cursor", builder=_Builder())
        invalid_cursor = client.get(
            f"/api/research/roadmap/runs/{view.run_id}/timeline?cursor=not-a-cursor"
        )
        assert (invalid_cursor.status_code, invalid_cursor.json()["detail"]) == (
            400,
            "ROADMAP_INVALID_CURSOR",
        )
        assert is_remote_path_allowed("/api/research/roadmap")
        assert is_remote_path_allowed("/api/research/roadmap/runs/run-1")
        assert is_remote_path_allowed("/api/research/roadmap/runs/run-1/timeline")
        assert not is_remote_path_allowed("/api/research/roadmap/../config")
    finally:
        database.dispose()


def test_source_metadata_is_whitelisted_and_remote_auth_is_unchanged(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, "privacy", builder=_Builder())
        with database.session() as session:
            row = session.scalar(
                select(ResearchPipelineRunRecord).where(
                    ResearchPipelineRunRecord.run_id == view.run_id
                )
            )
            row.requested_inputs_json = {
                **row.requested_inputs_json,
                "source_logical_id": r"C:\Users\operator\.env",
                "password": "secret-value",
            }
            row.source_references_json = [
                {"reference_type": "source_population", "reference_id": r"D:\Project_001\secret.db"}
            ]
            artifact = session.scalar(select(ResearchArtifactRecord))
            artifact.artifact_id = r"C:\Users\operator\artifact.jsonl"
            artifact.logical_locator = r"D:\Project_001\private\artifact.jsonl"
        payload = get_roadmap_run(database, view.run_id).model_dump_json()
        assert "C:\\Users" not in payload
        assert "secret.db" not in payload
        assert "secret-value" not in payload
        assert "artifact.jsonl" not in payload

        remote = _client(database, tmp_path, remote=True).get(
            "/api/research/roadmap", headers={"Tailscale-User-Login": "operator@example.com"}
        )
        assert remote.status_code in {401, 503}
    finally:
        database.dispose()


def test_blocked_and_waiting_states_are_not_remapped(tmp_path):
    database = _database(tmp_path)
    try:
        blocked = _run(
            database,
            tmp_path,
            "blocked",
            builder=_Builder(session_aware=False),
        )
        assert get_roadmap_run(database, blocked.run_id).current_run.lifecycle_state == "BLOCKED"
        waiting = create_pipeline_run(
            database,
            pipeline_version="waiting_pipeline_v1",
            experiment_key="waiting",
            idempotency_key="waiting",
        )
        assert get_roadmap_run(database, waiting.run_id).current_run.lifecycle_state == "WAITING"
    finally:
        database.dispose()


def test_roadmap_uses_bounded_queries_and_performs_no_writes(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, "query-count", builder=_Builder())
        statements: list[str] = []

        def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().upper())

        event.listen(database.engine, "before_cursor_execute", observe)
        try:
            get_roadmap_run(database, view.run_id)
        finally:
            event.remove(database.engine, "before_cursor_execute", observe)
        assert len([statement for statement in statements if statement.startswith("SELECT")]) <= 3
        assert not any(
            statement.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
            for statement in statements
        )
    finally:
        database.dispose()


def test_read_api_preserves_persisted_snapshot_and_rejects_mutations(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, "snapshot", builder=_Builder((_row("one"),)), total=1)

        def snapshot():
            with database.session() as session:
                return (
                    tuple(
                        (
                            row.id,
                            row.run_id,
                            row.status,
                            row.requested_inputs_json,
                            row.source_references_json,
                            row.execution_allowed,
                        )
                        for row in session.scalars(select(ResearchPipelineRunRecord)).all()
                    ),
                    tuple(
                        (
                            row.id,
                            row.stage_run_id,
                            row.status,
                            row.progress_processed,
                            row.progress_total,
                            row.gate_evidence_json,
                            row.lineage_references_json,
                            row.execution_allowed,
                        )
                        for row in session.scalars(select(ResearchStageRunRecord)).all()
                    ),
                    tuple(
                        (
                            row.id,
                            row.artifact_id,
                            row.validation_status,
                            row.publication_status,
                            row.logical_locator,
                            row.lineage_references_json,
                            row.execution_allowed,
                        )
                        for row in session.scalars(select(ResearchArtifactRecord)).all()
                    ),
                )

        before = snapshot()
        client = _client(database, tmp_path)
        assert client.get("/api/research/roadmap").status_code == 200
        assert client.get(f"/api/research/roadmap/runs/{view.run_id}").status_code == 200
        assert (
            client.get(f"/api/research/roadmap/runs/{view.run_id}/timeline").status_code
            == 200
        )
        assert client.post("/api/research/roadmap").status_code == 405
        assert client.delete("/api/research/roadmap").status_code == 405
        assert snapshot() == before
    finally:
        database.dispose()


def test_malformed_persisted_evidence_fails_safely_without_echoing_values(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, "malformed", builder=_Builder())
        with database.session() as session:
            pipeline = session.scalar(
                select(ResearchPipelineRunRecord).where(
                    ResearchPipelineRunRecord.run_id == view.run_id
                )
            )
            stage = session.scalar(
                select(ResearchStageRunRecord).where(
                    ResearchStageRunRecord.pipeline_run_id == pipeline.id,
                    ResearchStageRunRecord.stage_key == "DATASET_BUILD",
                )
            )
            stage.gate_evidence_json = [r"C:\Users\secret\malformed-evidence"]
        with pytest.raises(RoadmapError) as error:
            get_roadmap_run(database, view.run_id)
        assert error.value.code == "ROADMAP_QUERY_FAILED"
        assert "secret" not in str(error.value)
        assert "malformed-evidence" not in str(error.value)
    finally:
        database.dispose()


def test_roadmap_service_has_no_builder_or_runtime_authority():
    source = Path("services/research_roadmap.py").read_text().lower()
    assert "modelinferencedatasetbuilder" not in source
    assert "services.mt5" not in source
    assert "broker" not in source
    assert "transition_pipeline" not in source
    assert "retry_stage_attempt" not in source
