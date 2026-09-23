"""Persist versioned Phase 3 strategy-intelligence evidence snapshots."""

import sqlalchemy as sa

from alembic import op


revision = "20260923_0011"
down_revision = "20260922_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_intelligence_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_id", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("evidence_version", sa.String(64), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("timeframe", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence_band", sa.String(16), nullable=False),
        sa.Column("alert_decision", sa.String(16), nullable=False),
        sa.Column("blockers_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("score_components_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("forward_session_id", sa.String(36), sa.ForeignKey("forward_validation_sessions.id")),
        sa.Column("forward_signal_id", sa.String(36), sa.ForeignKey("forward_validation_signals.id")),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("candidate_id", name="uq_strategy_intelligence_candidate"),
    )
    op.create_index(
        "ix_strategy_intelligence_symbol_time",
        "strategy_intelligence_records",
        ["symbol", "detected_at"],
    )
    op.create_index(
        "ix_strategy_intelligence_alert",
        "strategy_intelligence_records",
        ["alert_decision", "detected_at"],
    )
    op.create_index(
        "ix_strategy_intelligence_records_candidate_id",
        "strategy_intelligence_records",
        ["candidate_id"],
    )
    op.create_index(
        "ix_strategy_intelligence_records_forward_session_id",
        "strategy_intelligence_records",
        ["forward_session_id"],
    )
    op.create_index(
        "ix_strategy_intelligence_records_forward_signal_id",
        "strategy_intelligence_records",
        ["forward_signal_id"],
    )


def downgrade() -> None:
    op.drop_table("strategy_intelligence_records")
