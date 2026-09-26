from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from alembic import command
from models.research_pipeline import (
    EmbeddedReferenceV1,
    QualityGateEvidenceV1,
    ResearchArtifact,
    ResearchPipelineRun,
)
from persistence.database import Database
from persistence.orm import (
    ResearchPipelineRunRecord,
    ResearchStageRunRecord,
)
from services.research_pipeline import (
    ArtifactContentConflict,
    ArtifactIdentityConflict,
    IdempotencyConflict,
    IllegalResearchTransition,
    TransitionConflict,
    _transition_pipeline_expected,
    _transition_stage_expected,
    create_pipeline_run,
    create_stage_attempt,
    register_artifact,
    retry_stage_attempt,
    transition_stage,
    update_stage_progress,
)


def _database(tmp_path: Path) -> Database:
    path = tmp_path / "research-pipeline.db"
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "head")
    return Database(f"sqlite:///{path.as_posix()}")


def _pipeline(database: Database, *, key: str = "same-key") -> ResearchPipelineRunRecord:
    return create_pipeline_run(
        database,
        pipeline_version="roadmap_v1",
        experiment_key="experiment-a",
        idempotency_key=key,
        requested_inputs={"symbol": "XAUUSD", "timeframe": "M15"},
        source_references=(EmbeddedReferenceV1(reference_type="source", reference_id="source-1"),),
    )


def _stage(database: Database, pipeline: ResearchPipelineRunRecord, *, status: str = "READY"):
    return create_stage_attempt(
        database,
        pipeline_run_id=pipeline.run_id,
        stage_key="DATASET_AUDIT",
        stage_version="v1",
        status=status,
    )


def test_pipeline_idempotency_and_conflict(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        first = _pipeline(database)
        second = _pipeline(database)
        assert first.id == second.id
        with pytest.raises(IdempotencyConflict):
            create_pipeline_run(
                database,
                pipeline_version="roadmap_v1",
                experiment_key="different",
                idempotency_key="same-key",
                requested_inputs={"symbol": "EURUSD"},
            )
    finally:
        database.dispose()


def test_concurrent_duplicate_creation_reuses_one_pipeline(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:

        def create() -> str:
            return _pipeline(database, key="concurrent-key").id

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _item: create(), range(2)))
        assert results[0] == results[1]
        with database.session() as session:
            assert (
                session.scalar(
                    select(ResearchPipelineRunRecord).where(
                        ResearchPipelineRunRecord.id == results[0]
                    )
                )
                is not None
            )
    finally:
        database.dispose()


def test_pipeline_database_idempotency_constraint(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        first = _pipeline(database)
        with pytest.raises(IntegrityError), database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO research_pipeline_runs "
                "(id, run_id, contract_version, pipeline_version, "
                "experiment_key, idempotency_key, "
                "request_fingerprint, requested_inputs_json, source_references_json, "
                "configuration_references_json, code_references_json, status, "
                "created_at, execution_allowed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "raw-duplicate",
                    "raw-run",
                    "research_pipeline_run_v1",
                    "v1",
                    "e",
                    "same-key",
                    "a" * 64,
                    "{}",
                    "[]",
                    "[]",
                    "[]",
                    "WAITING",
                    datetime.now(UTC),
                    0,
                ),
            )
        assert first.run_id
    finally:
        database.dispose()


@pytest.mark.parametrize("target", ["READY", "RUNNING", "PASS", "FAILED", "BLOCKED"])
def test_state_machine_transitions(tmp_path: Path, target: str) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key=f"key-{target}")
        stage = _stage(database, pipeline, status="WAITING" if target == "READY" else "READY")
        if target == "READY":
            result = transition_stage(database, stage.stage_run_id, "READY")
        elif target == "RUNNING":
            result = transition_stage(database, stage.stage_run_id, "RUNNING")
        else:
            transition_stage(database, stage.stage_run_id, "RUNNING")
            result = transition_stage(database, stage.stage_run_id, target, reason="TEST")
        assert result.status == target
    finally:
        database.dispose()


def test_illegal_and_terminal_transitions_fail_closed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key="state-key")
        stage = _stage(database, pipeline)
        with pytest.raises(IllegalResearchTransition):
            transition_stage(database, stage.stage_run_id, "PASS")
        transition_stage(database, stage.stage_run_id, "RUNNING")
        transition_stage(database, stage.stage_run_id, "PASS")
        with pytest.raises(IllegalResearchTransition):
            transition_stage(database, stage.stage_run_id, "READY")
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("initial", "left", "right"),
    [
        ("READY", "RUNNING", "BLOCKED"),
        ("READY", "BLOCKED", "CANCELLED"),
        ("RUNNING", "PASS", "FAILED"),
        ("RUNNING", "PASS", "BLOCKED"),
        ("RUNNING", "PASS", "CANCELLED"),
    ],
)
def test_concurrent_stage_transitions_are_compare_and_swap(
    tmp_path: Path, initial: str, left: str, right: str
) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key=f"stage-race-{left}-{right}")
        stage = _stage(database, pipeline)
        if initial == "RUNNING":
            transition_stage(database, stage.stage_run_id, "RUNNING")

        def attempt(target: str):
            try:
                result = _transition_stage_expected(
                    database, stage.stage_run_id, initial, target, reason="RACE"
                )
                return ("success", result.status)
            except TransitionConflict:
                return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, (left, right)))
        assert [result[0] for result in results].count("success") == 1
        assert [result[0] for result in results].count("conflict") == 1
        with database.session() as session:
            persisted = session.scalar(
                select(ResearchStageRunRecord).where(
                    ResearchStageRunRecord.stage_run_id == stage.stage_run_id
                )
            )
            assert persisted is not None
            assert persisted.status in {left, right}
    finally:
        database.dispose()


def test_concurrent_pipeline_transitions_are_compare_and_swap(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key="pipeline-race")

        def attempt(target: str):
            try:
                result = _transition_pipeline_expected(
                    database, pipeline.run_id, "WAITING", target, reason="RACE"
                )
                return ("success", result.status)
            except TransitionConflict:
                return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("READY", "CANCELLED")))
        assert [result[0] for result in results].count("success") == 1
        assert [result[0] for result in results].count("conflict") == 1
        with database.session() as session:
            persisted = session.scalar(
                select(ResearchPipelineRunRecord).where(
                    ResearchPipelineRunRecord.run_id == pipeline.run_id
                )
            )
            assert persisted is not None
            assert persisted.status in {"READY", "CANCELLED"}
    finally:
        database.dispose()


def test_metadata_security_rejects_secrets_and_private_paths_without_echoing() -> None:
    timestamp = datetime.now(UTC)
    cases = (
        lambda: ResearchPipelineRun(
            pipeline_version="v1",
            experiment_key="experiment",
            idempotency_key="request",
            request_fingerprint="a" * 64,
            requested_inputs={"source": r"C:\Users\operator\secret.txt"},
            created_at=timestamp,
        ),
        lambda: ResearchPipelineRun(
            pipeline_version="v1",
            experiment_key="experiment",
            idempotency_key="request",
            request_fingerprint="a" * 64,
            requested_inputs={"api_key": "super-secret-value"},
            created_at=timestamp,
        ),
        lambda: EmbeddedReferenceV1(
            reference_type="source", reference_id=r"\\server\share\file"
        ),
        lambda: EmbeddedReferenceV1(
            reference_type="source", reference_id="/home/user/private/file"
        ),
        lambda: EmbeddedReferenceV1(
            reference_type="source", reference_id="file:///private/source"
        ),
        lambda: QualityGateEvidenceV1(
            gate_id="audit", status="FAILED", measured={"authorization": "Bearer abc123"}
        ),
        lambda: QualityGateEvidenceV1(
            gate_id="audit",
            status="FAILED",
            measured={"material": "-----BEGIN PRIVATE KEY-----"},
        ),
    )
    sensitive_fragments = (
        "secret.txt",
        "super-secret-value",
        "server",
        "private",
        "abc123",
        "PRIVATE KEY",
    )
    for factory in cases:
        with pytest.raises(ValueError, match="unsafe metadata|safe logical") as error:
            factory()
        message = str(error.value)
        assert not any(
            fragment.casefold() in message.casefold() for fragment in sensitive_fragments
        )


def test_metadata_security_accepts_safe_logical_identifiers() -> None:
    timestamp = datetime.now(UTC)
    run = ResearchPipelineRun(
        run_id="run-001",
        pipeline_version="roadmap_v1",
        experiment_key="model_training_features_v1",
        idempotency_key="candidate-001",
        request_fingerprint="a" * 64,
        requested_inputs={
            "dataset": "dataset_v1",
            "symbol": "XAUUSDm",
            "timeframe": "M15",
            "candidate_id": "candidate-001",
            "content_sha256": "a" * 64,
        },
        source_references=(
            EmbeddedReferenceV1(reference_type="artifact", reference_id="dataset_v1"),
        ),
        created_at=timestamp,
    )
    gate = QualityGateEvidenceV1(
        gate_id="stage_quality",
        status="PASS",
        measured={"rows": 100, "version_id": "v1"},
        reason_codes=("DATASET_VALID",),
    )
    artifact = ResearchArtifact(
        artifact_id="artifact-001",
        artifact_kind="manifest",
        content_sha256="b" * 64,
        artifact_format="json",
        size_bytes=10,
        logical_locator="artifact-001",
        pipeline_run_id=run.run_id,
        producer_stage_run_id="stage-001",
        gate_evidence=(gate,),
        created_at=timestamp,
    )
    assert artifact.logical_locator == "artifact-001"


@pytest.mark.parametrize("terminal", ["FAILED", "BLOCKED"])
def test_retry_preserves_attempt_history(tmp_path: Path, terminal: str) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key=f"retry-{terminal}")
        stage = _stage(database, pipeline)
        transition_stage(database, stage.stage_run_id, "RUNNING")
        transition_stage(database, stage.stage_run_id, terminal, reason="DEPENDENCY")
        retry = retry_stage_attempt(database, stage.stage_run_id)
        assert retry.attempt_number == 2
        assert retry.status == "READY"
        with database.session() as session:
            rows = session.scalars(
                select(ResearchStageRunRecord).where(
                    ResearchStageRunRecord.pipeline_run_id == pipeline.id
                )
            ).all()
            assert [(row.attempt_number, row.status) for row in rows] == [
                (1, terminal),
                (2, "READY"),
            ]
    finally:
        database.dispose()


def test_progress_known_indeterminate_and_invalid(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key="progress-key")
        stage = _stage(database, pipeline)
        update_stage_progress(
            database, stage.stage_run_id, processed=48_231, total=100_000, unit="candidates"
        )
        update_stage_progress(
            database, stage.stage_run_id, processed=48_231, total=None, unit="candidates"
        )
        with pytest.raises(ValueError):
            update_stage_progress(database, stage.stage_run_id, processed=4, total=3, unit="rows")
        with pytest.raises(ValueError):
            update_stage_progress(database, stage.stage_run_id, processed=1, total=2, unit=None)
    finally:
        database.dispose()


def test_artifact_registration_bounds_identity_and_lineage(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        pipeline = _pipeline(database, key="artifact-key")
        stage = _stage(database, pipeline)
        gate = QualityGateEvidenceV1(gate_id="audit", status="PASS", measured={"rows": 10})
        artifact = register_artifact(
            database,
            pipeline_run_id=pipeline.run_id,
            producer_stage_run_id=stage.stage_run_id,
            artifact_id="artifact-1",
            artifact_kind="manifest",
            content_sha256="a" * 64,
            artifact_format="json",
            size_bytes=10,
            logical_locator="artifact-1",
            gate_evidence=(gate,),
            lineage_references=(
                EmbeddedReferenceV1(reference_type="dataset", reference_id="dataset-1"),
            ),
        )
        assert artifact.publication_status == "UNPUBLISHED"
        assert artifact.gate_evidence_json[0]["gate_id"] == "audit"
        assert (
            register_artifact(
                database,
                pipeline_run_id=pipeline.run_id,
                producer_stage_run_id=stage.stage_run_id,
                artifact_id="artifact-1",
                artifact_kind="manifest",
                content_sha256="a" * 64,
                artifact_format="json",
                size_bytes=10,
                logical_locator="artifact-1",
            ).id
            == artifact.id
        )
        with pytest.raises(ArtifactIdentityConflict):
            register_artifact(
                database,
                pipeline_run_id=pipeline.run_id,
                producer_stage_run_id=stage.stage_run_id,
                artifact_id="artifact-1",
                artifact_kind="manifest",
                content_sha256="b" * 64,
                artifact_format="json",
                size_bytes=10,
                logical_locator="artifact-1",
            )
        with pytest.raises(ArtifactContentConflict):
            register_artifact(
                database,
                pipeline_run_id=pipeline.run_id,
                producer_stage_run_id=stage.stage_run_id,
                artifact_id="artifact-2",
                artifact_kind="manifest",
                content_sha256="a" * 64,
                artifact_format="json",
                size_bytes=10,
                logical_locator="artifact-2",
            )
    finally:
        database.dispose()


def test_contract_bounds_and_no_execution_dependency() -> None:
    with pytest.raises(ValueError):
        QualityGateEvidenceV1(
            gate_id="audit",
            status="PASS",
            evidence_references=tuple(
                EmbeddedReferenceV1(reference_type="source", reference_id=str(index))
                for index in range(17)
            ),
        )
    assert "services.demo_execution" not in Path("services/research_pipeline.py").read_text()
    assert "services.forward_shadow" not in Path("services/research_pipeline.py").read_text()
    assert "mt5" not in Path("services/research_pipeline.py").read_text().lower()


def test_migration_round_trip_and_unrelated_data_preserved(tmp_path: Path) -> None:
    database_path = tmp_path / "round-trip.db"
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "20260925_0019")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO configuration_versions "
                "(id, version, timestamp, configuration, checksum, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("keep", "v1", "2026-09-26 00:00:00", "{}", "c" * 64, 1),
            )
    finally:
        database.dispose()
    command.upgrade(config, "20260926_0020")
    assert {"research_pipeline_runs", "research_stage_runs", "research_artifacts"} <= set(
        inspect(Database(f"sqlite:///{database_path.as_posix()}").engine).get_table_names()
    )
    command.downgrade(config, "20260925_0019")
    assert (
        "research_pipeline_runs"
        not in inspect(Database(f"sqlite:///{database_path.as_posix()}").engine).get_table_names()
    )
    command.upgrade(config, "20260926_0020")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version FROM configuration_versions WHERE id = ?", ("keep",)
                ).scalar_one()
                == "v1"
            )
    finally:
        database.dispose()
