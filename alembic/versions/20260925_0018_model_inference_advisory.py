"""Persist deterministic offline model-inference advisory evaluations."""

import sqlalchemy as sa

from alembic import op

revision = "20260925_0018"
down_revision = "20260925_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_inference_evaluations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_key", sa.String(length=64), nullable=False),
        sa.Column("inference_version", sa.String(length=64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("source_intelligence_record_id", sa.String(length=36), nullable=False),
        sa.Column("source_candidate_id", sa.String(length=100), nullable=False),
        sa.Column("forward_session_id", sa.String(length=36), nullable=True),
        sa.Column("forward_signal_id", sa.String(length=36), nullable=True),
        sa.Column("pair_zone_event_id", sa.String(length=100), nullable=True),
        sa.Column("strategy_config_hash", sa.String(length=64), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("advisory_classification", sa.String(length=32), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "execution_allowed = false", name="ck_model_inference_advisory_only"
        ),
        sa.CheckConstraint(
            "advisory_classification IN ('SUPPORTIVE', 'CAUTION', 'OBSERVATION_ONLY')",
            name="ck_model_inference_advisory_classification",
        ),
        sa.ForeignKeyConstraint(
            ["source_intelligence_record_id"], ["strategy_intelligence_records.id"]
        ),
        sa.ForeignKeyConstraint(["forward_session_id"], ["forward_validation_sessions.id"]),
        sa.ForeignKeyConstraint(["forward_signal_id"], ["forward_validation_signals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_key", name="uq_model_inference_evaluation_key"),
    )
    op.create_index(
        "ix_model_inference_session_time",
        "model_inference_evaluations",
        ["forward_session_id", "evaluated_at"],
    )
    for column in (
        "source_intelligence_record_id",
        "source_candidate_id",
        "forward_signal_id",
        "pair_zone_event_id",
    ):
        op.create_index(
            f"ix_model_inference_evaluations_{column}",
            "model_inference_evaluations",
            [column],
        )


def downgrade() -> None:
    for column in (
        "source_intelligence_record_id",
        "source_candidate_id",
        "forward_signal_id",
        "pair_zone_event_id",
    ):
        op.drop_index(
            f"ix_model_inference_evaluations_{column}",
            table_name="model_inference_evaluations",
        )
    op.drop_index(
        "ix_model_inference_session_time", table_name="model_inference_evaluations"
    )
    op.drop_table("model_inference_evaluations")
