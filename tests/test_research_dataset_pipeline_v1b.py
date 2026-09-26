from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

import services.research_dataset_pipeline as pipeline_service
from models.model_inference_dataset import DatasetRowV1
from persistence.database import Database
from services.model_inference_dataset import _stable
from services.research_dataset_pipeline import (
    get_dataset_pipeline_view,
    retry_model_inference_dataset_pipeline,
    run_model_inference_dataset_pipeline,
)
from services.research_pipeline import IdempotencyConflict


class _FakeBuilder:
    page_size = 2
    session_aware_v2_accepted = True

    def __init__(self, rows=(), *, failure: bool = False) -> None:
        self.rows = tuple(rows)
        self.failure = failure

    def iter_rows(self):
        if self.failure:
            raise RuntimeError("builder fixture failure")
        yield from self.rows


def _database(tmp_path: Path) -> Database:
    database = Database.for_test(f"sqlite:///{(tmp_path / 'pipeline.db').as_posix()}")
    database.create_test_schema()
    return database


def _row(candidate_id: str, *, trainable: bool = False) -> DatasetRowV1:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    payload = {
        "dataset_contract_version": "model_inference_dataset_v1",
        "feature_contract_version": "model_training_features_v1",
        "candidate_id": candidate_id,
        "symbol": "XAUUSD",
        "candidate_timestamp": now.isoformat(),
        "causal_cutoff_timestamp": now.isoformat(),
        "state": "OUTCOME_COMPLETED" if trainable else "PRE_SIGNAL_OBSERVATION",
        "training_eligibility": "TRAINABLE" if trainable else "NON_TRAINABLE",
        "reason_codes": () if trainable else ("PRE_SIGNAL",),
        "classification": None,
        "outcome_label": "TP" if trainable else "NOT_ELIGIBLE",
        "features": {},
        "source_provenance": {"candidate_id": candidate_id},
        "source_timestamps": {},
        "row_identity": hashlib.sha256(candidate_id.encode()).hexdigest(),
    }
    candidate = DatasetRowV1(**payload, row_fingerprint="0" * 64)
    canonical_payload = candidate.model_dump(mode="json")
    canonical_payload.pop("row_fingerprint")
    return candidate.model_copy(
        update={"row_fingerprint": hashlib.sha256(_stable(canonical_payload).encode()).hexdigest()}
    )


def _run(
    database,
    tmp_path: Path,
    *,
    key: str,
    builder=None,
    total=None,
    source_logical_id: str = "strategy_intelligence_forward_evidence_v1",
):
    return run_model_inference_dataset_pipeline(
        database,
        idempotency_key=key,
        output_dir=tmp_path / "artifacts",
        builder=builder,
        total_candidates=total,
        source_logical_id=source_logical_id,
    )


def _rewrite_jsonl(path: Path, transform) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(f"{_stable(transform(row))}\n" for row in rows))


def _rewrite_manifest(path: Path, transform) -> None:
    manifest = json.loads(path.read_text())
    path.write_text(_stable(transform(manifest)))


def test_empty_dataset_completes_with_indeterminate_progress_and_two_artifacts(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, key="empty")
        assert view.status == "PASS"
        assert [stage.stage_key for stage in view.stages] == [
            "DATA_SOURCE",
            "DATASET_BUILD",
            "DATASET_AUDIT",
            "ARTIFACT_VERIFY",
        ]
        assert view.summary is not None
        assert view.summary.rows_produced == 0
        assert len(view.artifacts) == 2
        assert all(artifact.validation_status == "VALID" for artifact in view.artifacts)
        build = view.stages[1]
        assert build.processed == 0
        assert build.total is None
        assert build.progress_mode == "INDETERMINATE"
        assert str(tmp_path) not in view.model_dump_json()
    finally:
        database.dispose()


def test_non_empty_mixed_rows_and_known_progress_reconcile(tmp_path):
    database = _database(tmp_path)
    try:
        builder = _FakeBuilder((_row("non-trainable"), _row("trainable", trainable=True)))
        view = _run(database, tmp_path, key="mixed", builder=builder, total=2)
        assert view.status == "PASS"
        assert view.summary is not None
        assert view.summary.rows_produced == 2
        assert view.summary.trainable_rows == 1
        assert view.summary.non_trainable_rows == 1
        build = view.stages[1]
        assert (build.processed, build.total, build.progress_mode) == (2, 2, "KNOWN")
    finally:
        database.dispose()


def test_zero_total_is_explicit_without_fake_percentage(tmp_path):
    database = _database(tmp_path)
    try:
        view = _run(database, tmp_path, key="zero", builder=_FakeBuilder(), total=0)
        build = view.stages[1]
        assert view.status == "PASS"
        assert build.processed == 0
        assert build.total == 0
        assert build.progress_mode == "KNOWN"
        assert "percentage" not in view.model_dump_json().lower()
    finally:
        database.dispose()


def test_builder_failure_is_failed_and_retry_creates_attempt_two(tmp_path):
    database = _database(tmp_path)
    try:
        failed = _run(database, tmp_path, key="retry", builder=_FakeBuilder(failure=True))
        assert failed.status == "FAILED"
        assert failed.stages[1].status == "FAILED"
        recovered = retry_model_inference_dataset_pipeline(
            database,
            run_id=failed.run_id,
            output_dir=tmp_path / "artifacts",
            builder=_FakeBuilder((_row("recovered"),)),
        )
        assert recovered.status == "PASS"
        build_attempts = [stage for stage in recovered.stages if stage.stage_key == "DATASET_BUILD"]
        assert [stage.attempt_number for stage in build_attempts] == [1, 2]
        assert build_attempts[0].status == "FAILED"
        assert build_attempts[1].status == "PASS"
        assert all(
            artifact.producer_stage_run_id == build_attempts[1].stage_run_id
            for artifact in recovered.artifacts
        )
    finally:
        database.dispose()


def test_continuity_source_contract_is_blocked_when_builder_does_not_accept_v2(tmp_path):
    database = _database(tmp_path)
    try:
        builder = _FakeBuilder()
        builder.session_aware_v2_accepted = False
        view = _run(database, tmp_path, key="continuity-block", builder=builder)
        assert view.status == "BLOCKED"
        assert view.stages[1].status == "BLOCKED"
    finally:
        database.dispose()


def test_idempotency_reuses_completed_run_and_changed_request_conflicts(tmp_path):
    database = _database(tmp_path)
    try:
        first = _run(database, tmp_path, key="reuse", builder=_FakeBuilder())
        second = _run(database, tmp_path, key="reuse", builder=_FakeBuilder((_row("new"),)))
        assert second.run_id == first.run_id
        assert len(second.artifacts) == 2
        with pytest.raises(IdempotencyConflict):
            run_model_inference_dataset_pipeline(
                database,
                idempotency_key="reuse",
                output_dir=tmp_path / "other",
                symbol="XAUUSDm",
            )
    finally:
        database.dispose()


def test_concurrent_duplicate_invocation_has_one_authoritative_pipeline(tmp_path):
    database = _database(tmp_path)
    try:
        def invoke(_index):
            return _run(database, tmp_path, key="concurrent", builder=_FakeBuilder())

        with ThreadPoolExecutor(max_workers=2) as executor:
            views = list(executor.map(invoke, range(2)))
        assert views[0].run_id == views[1].run_id
        final = get_dataset_pipeline_view(database, views[0].run_id)
        assert final.status == "PASS"
        assert len(final.stages) == 4
        assert len(final.artifacts) == 2
    finally:
        database.dispose()


def test_duplicate_identity_and_leakage_are_rejected_by_audit(tmp_path):
    database = _database(tmp_path)
    try:
        duplicate = _run(
            database,
            tmp_path,
            key="duplicate",
            builder=_FakeBuilder((_row("same"), _row("same"))),
        )
        assert duplicate.status == "FAILED"
        assert duplicate.stages[2].status == "FAILED"
        assert all(artifact.validation_status == "UNVALIDATED" for artifact in duplicate.artifacts)

        leaking = _row("leak")
        leaking = leaking.model_copy(update={"features": {"gross_r": 1.0}})
        leaking_payload = leaking.model_dump(mode="json")
        fingerprint = leaking_payload.copy()
        fingerprint.pop("row_fingerprint")
        leaking = leaking.model_copy(
            update={"row_fingerprint": hashlib.sha256(_stable(fingerprint).encode()).hexdigest()}
        )
        rejected = _run(
            database,
            tmp_path,
            key="leakage",
            builder=_FakeBuilder((leaking,)),
        )
        assert rejected.status == "FAILED"
    finally:
        database.dispose()


def test_artifact_hash_mismatch_fails_audit(tmp_path, monkeypatch):
    database = _database(tmp_path)
    original = pipeline_service._audit_files

    def tamper(jsonl_path, manifest_path, **kwargs):
        jsonl_path.write_bytes(jsonl_path.read_bytes() + b"tampered\n")
        return original(jsonl_path, manifest_path, **kwargs)

    monkeypatch.setattr(pipeline_service, "_audit_files", tamper)
    try:
        view = _run(database, tmp_path, key="artifact-hash", builder=_FakeBuilder((_row("hash"),)))
        assert view.status == "FAILED"
        assert view.stages[2].status == "FAILED"
    finally:
        database.dispose()


def test_manifest_hash_mismatch_fails_audit(tmp_path, monkeypatch):
    database = _database(tmp_path)
    original = pipeline_service._audit_files

    def tamper(jsonl_path, manifest_path, **kwargs):
        manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        return original(jsonl_path, manifest_path, **kwargs)

    monkeypatch.setattr(pipeline_service, "_audit_files", tamper)
    try:
        view = _run(
            database,
            tmp_path,
            key="manifest-hash",
            builder=_FakeBuilder((_row("manifest"),)),
        )
        assert view.status == "FAILED"
        assert view.stages[2].status == "FAILED"
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("case", "builder_rows"),
    [
        ("jsonl-content", (_row("jsonl"),)),
        ("manifest-content", (_row("manifest-content"),)),
        ("artifact-sha", (_row("artifact-sha"),)),
        ("semantic-hash", (_row("semantic"),)),
        ("row-count", (_row("row-count"),)),
        ("duplicate-candidate", (_row("duplicate"), _row("duplicate"))),
        ("duplicate-fingerprint", (_row("fingerprint-a"), _row("fingerprint-b"))),
        ("changed-fingerprint", (_row("changed-fingerprint"),)),
        ("canonical-json", (_row("canonical"),)),
        ("contract-version", (_row("contract"),)),
        ("eligibility", (_row("eligibility"),)),
        ("leakage", (_row("leakage"),)),
        ("symbol", (_row("symbol"),)),
        ("continuity", (_row("continuity"),)),
        ("truncated", (_row("truncated-a"), _row("truncated-b"))),
        ("extra", (_row("extra-base"),)),
        ("missing", (_row("missing-a"), _row("missing-b"))),
    ],
)
def test_audit_corruption_matrix_fails_closed(
    tmp_path, monkeypatch, case, builder_rows
):
    database = _database(tmp_path)
    original = pipeline_service._audit_files

    def tamper(jsonl_path, manifest_path, **kwargs):
        if case == "jsonl-content":
            jsonl_path.write_bytes(jsonl_path.read_bytes() + b"changed\n")
        elif case == "manifest-content":
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        elif case == "artifact-sha":
            kwargs["expected_artifact_sha256"] = "0" * 64
        elif case == "semantic-hash":
            _rewrite_manifest(manifest_path, lambda value: {**value, "dataset_hash": "0" * 64})
            kwargs["expected_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
        elif case == "row-count":
            def change_count(value):
                return {**value, "row_count": value["row_count"] + 1}

            _rewrite_manifest(manifest_path, change_count)
            kwargs["expected_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
        elif case == "duplicate-fingerprint":
            rows = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
            rows[1]["row_fingerprint"] = rows[0]["row_fingerprint"]
            jsonl_path.write_text("".join(f"{_stable(row)}\n" for row in rows))
        elif case == "changed-fingerprint":
            _rewrite_jsonl(jsonl_path, lambda value: {**value, "row_fingerprint": "0" * 64})
        elif case == "canonical-json":
            raw = jsonl_path.read_text()
            jsonl_path.write_text(" " + raw)
        elif case == "contract-version":
            _rewrite_jsonl(
                jsonl_path,
                lambda value: {**value, "dataset_contract_version": "unexpected_v1"},
            )
        elif case == "eligibility":
            _rewrite_jsonl(
                jsonl_path,
                lambda value: {**value, "training_eligibility": "TRAINABLE"},
            )
        elif case == "leakage":
            _rewrite_jsonl(
                jsonl_path,
                lambda value: {**value, "features": {"gross_r": 1.0}},
            )
        elif case == "symbol":
            _rewrite_jsonl(jsonl_path, lambda value: {**value, "symbol": "XAUUSDm"})
        elif case == "continuity":
            _rewrite_manifest(
                manifest_path,
                lambda value: {**value, "session_aware_v2_accepted": False},
            )
            kwargs["expected_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
        elif case == "truncated":
            lines = jsonl_path.read_text().splitlines(keepends=True)
            jsonl_path.write_text("".join(lines[:-1]))
        elif case == "extra":
            extra = _row("extra-row").model_dump(mode="json")
            jsonl_path.write_text(
                jsonl_path.read_text() + f"{_stable(extra)}\n"
            )
        elif case == "missing":
            lines = jsonl_path.read_text().splitlines(keepends=True)
            jsonl_path.write_text("".join(lines[1:]))
        return original(jsonl_path, manifest_path, **kwargs)

    monkeypatch.setattr(pipeline_service, "_audit_files", tamper)
    try:
        view = _run(
            database,
            tmp_path,
            key=f"corruption-{case}",
            builder=_FakeBuilder(builder_rows),
        )
        assert view.status == "FAILED"
        assert view.stages[2].status == "FAILED"
        assert all(
            artifact.validation_status == "UNVALIDATED"
            and artifact.publication_status == "UNPUBLISHED"
            for artifact in view.artifacts
        )
    finally:
        database.dispose()


def test_audit_failure_retry_creates_historical_audit_attempt(tmp_path, monkeypatch):
    database = _database(tmp_path)
    original = pipeline_service._audit_files
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            report = original(*args, **kwargs)
            return report.model_copy(
                update={
                    "artifact_sha256_matches": False,
                    "failure_codes": ("ARTIFACT_SHA256_MISMATCH",),
                }
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline_service, "_audit_files", fail_once)
    try:
        failed = _run(database, tmp_path, key="audit-retry", builder=_FakeBuilder())
        assert failed.status == "FAILED"
        recovered = retry_model_inference_dataset_pipeline(
            database, run_id=failed.run_id, output_dir=tmp_path / "artifacts"
        )
        assert recovered.status == "PASS"
        audit_attempts = [
            stage for stage in recovered.stages if stage.stage_key == "DATASET_AUDIT"
        ]
        assert [stage.attempt_number for stage in audit_attempts] == [1, 2]
        assert audit_attempts[0].status == "FAILED"
        assert audit_attempts[1].status == "PASS"
    finally:
        database.dispose()


def test_sensitive_source_metadata_is_rejected_without_echoing(tmp_path):
    database = _database(tmp_path)
    try:
        with pytest.raises(ValueError) as error:
            _run(
                database,
                tmp_path,
                key="private",
                source_logical_id=r"C:\Users\operator\secret.txt",
            )
        assert "secret.txt" not in str(error.value)
        assert "C:\\Users" not in str(error.value)
    finally:
        database.dispose()


def test_v1b_service_has_no_runtime_or_trading_authority():
    source = Path("services/research_dataset_pipeline.py").read_text().lower()
    assert "services.demo_execution" not in source
    assert "services.mt5" not in source
    assert "broker" not in source
    assert "submit_order" not in source
