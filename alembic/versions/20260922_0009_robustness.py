"""Persist read-only robustness and cost validation manifests."""

import sqlalchemy as sa

from alembic import op

revision = "20260922_0009"
down_revision = "20260922_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_robustness_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("robustness_id", sa.String(100), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column(
            "dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id"), nullable=False
        ),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("source_run_id", sa.String(36), sa.ForeignKey("research_runs.id")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("simulation_count", sa.Integer(), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("robustness_id", name="uq_research_robustness_id"),
    )
    op.create_index(
        "ix_research_robustness_runs_robustness_id", "research_robustness_runs", ["robustness_id"]
    )
    op.create_index(
        "ix_research_robustness_runs_strategy_id", "research_robustness_runs", ["strategy_id"]
    )
    op.create_index("ix_research_robustness_runs_status", "research_robustness_runs", ["status"])
    op.create_index("ix_research_robustness_created", "research_robustness_runs", ["created_at"])


def downgrade() -> None:
    op.drop_table("research_robustness_runs")
