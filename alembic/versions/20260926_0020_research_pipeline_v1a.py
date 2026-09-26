"""Add the read-only research pipeline V1A foundation."""

import sqlalchemy as sa

from alembic import op

revision = "20260926_0020"
down_revision = "20260925_0019"
branch_labels = None
depends_on = None

STATUSES = "'WAITING', 'READY', 'RUNNING', 'PASS', 'BLOCKED', 'FAILED', 'SKIPPED', 'CANCELLED'"


def upgrade() -> None:
    op.create_table(
        "research_pipeline_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_key", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("requested_inputs_json", sa.JSON(), nullable=False),
        sa.Column("source_references_json", sa.JSON(), nullable=False),
        sa.Column("configuration_references_json", sa.JSON(), nullable=False),
        sa.Column("code_references_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("terminal_reason", sa.String(length=200), nullable=True),
        sa.Column("execution_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_research_pipeline_status"),
        sa.CheckConstraint("execution_allowed = false", name="ck_research_pipeline_read_only"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_research_pipeline_run_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_research_pipeline_idempotency"),
    )
    op.create_index("ix_research_pipeline_runs_run_id", "research_pipeline_runs", ["run_id"])
    op.create_index("ix_research_pipeline_runs_status", "research_pipeline_runs", ["status"])
    op.create_index(
        "ix_research_pipeline_status_created",
        "research_pipeline_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "research_stage_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("stage_run_id", sa.String(length=100), nullable=False),
        sa.Column("pipeline_run_id", sa.String(length=36), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=64), nullable=False),
        sa.Column("stage_version", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress_processed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("progress_unit", sa.String(length=64), nullable=True),
        sa.Column("blocked_reason_code", sa.String(length=100), nullable=True),
        sa.Column("failure_reason_code", sa.String(length=100), nullable=True),
        sa.Column("terminal_reason", sa.String(length=200), nullable=True),
        sa.Column("input_references_json", sa.JSON(), nullable=False),
        sa.Column("evidence_references_json", sa.JSON(), nullable=False),
        sa.Column("gate_evidence_json", sa.JSON(), nullable=False),
        sa.Column("lineage_references_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("execution_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_research_stage_status"),
        sa.CheckConstraint(
            "progress_processed >= 0 AND (progress_total IS NULL OR "
            "(progress_total >= 0 AND progress_processed <= progress_total))",
            name="ck_research_stage_progress",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_research_stage_attempt_number"),
        sa.CheckConstraint("execution_allowed = false", name="ck_research_stage_read_only"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["research_pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stage_run_id", name="uq_research_stage_run_id"),
        sa.UniqueConstraint(
            "pipeline_run_id", "stage_key", "attempt_number", name="uq_research_stage_attempt"
        ),
    )
    op.create_index("ix_research_stage_runs_stage_run_id", "research_stage_runs", ["stage_run_id"])
    op.create_index("ix_research_stage_runs_status", "research_stage_runs", ["status"])
    op.create_index(
        "ix_research_stage_pipeline_status",
        "research_stage_runs",
        ["pipeline_run_id", "status"],
    )

    op.create_table(
        "research_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=100), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("artifact_kind", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_format", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("logical_locator", sa.String(length=200), nullable=False),
        sa.Column("pipeline_run_id", sa.String(length=36), nullable=False),
        sa.Column("producer_stage_run_id", sa.String(length=36), nullable=False),
        sa.Column("publication_status", sa.String(length=16), nullable=False),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("gate_evidence_json", sa.JSON(), nullable=False),
        sa.Column("lineage_references_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("execution_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_research_artifact_size"),
        sa.CheckConstraint(
            "publication_status IN ('UNPUBLISHED', 'PUBLISHED', 'REJECTED')",
            name="ck_research_artifact_publication",
        ),
        sa.CheckConstraint(
            "validation_status IN ('UNVALIDATED', 'VALID', 'INVALID')",
            name="ck_research_artifact_validation",
        ),
        sa.CheckConstraint("execution_allowed = false", name="ck_research_artifact_read_only"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["research_pipeline_runs.id"]),
        sa.ForeignKeyConstraint(["producer_stage_run_id"], ["research_stage_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", name="uq_research_artifact_id"),
        sa.UniqueConstraint("content_sha256", name="uq_research_artifact_content"),
    )
    op.create_index("ix_research_artifacts_artifact_id", "research_artifacts", ["artifact_id"])
    op.create_index(
        "ix_research_artifacts_content_sha256", "research_artifacts", ["content_sha256"]
    )
    op.create_index(
        "ix_research_artifacts_pipeline_created",
        "research_artifacts",
        ["pipeline_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_artifacts_pipeline_created", table_name="research_artifacts")
    op.drop_index("ix_research_artifacts_content_sha256", table_name="research_artifacts")
    op.drop_index("ix_research_artifacts_artifact_id", table_name="research_artifacts")
    op.drop_table("research_artifacts")
    op.drop_index("ix_research_stage_pipeline_status", table_name="research_stage_runs")
    op.drop_index("ix_research_stage_runs_status", table_name="research_stage_runs")
    op.drop_index("ix_research_stage_runs_stage_run_id", table_name="research_stage_runs")
    op.drop_table("research_stage_runs")
    op.drop_index("ix_research_pipeline_status_created", table_name="research_pipeline_runs")
    op.drop_index("ix_research_pipeline_runs_status", table_name="research_pipeline_runs")
    op.drop_index("ix_research_pipeline_runs_run_id", table_name="research_pipeline_runs")
    op.drop_table("research_pipeline_runs")
