"""Add isolated immutable historical research and backtest persistence."""
# ruff: noqa: E501

import sqlalchemy as sa

from alembic import op

revision = "20260922_0008"
down_revision = "20260922_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_datasets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(32), nullable=False),
        sa.Column("closed_candles_only", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("dataset_id", name="uq_research_datasets_dataset_id"),
        sa.UniqueConstraint("dataset_hash", name="uq_research_dataset_hash"),
    )
    op.create_index("ix_research_datasets_symbol_timeframe", "research_datasets", ["symbol", "timeframe"])
    op.create_index("ix_research_datasets_dataset_id", "research_datasets", ["dataset_id"])
    op.create_table(
        "research_candles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id"), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("tick_volume", sa.BigInteger(), nullable=False),
        sa.Column("spread", sa.Integer(), nullable=False),
        sa.Column("real_volume", sa.BigInteger(), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("dataset_id", "timestamp", name="uq_research_candle"),
    )
    op.create_index("ix_research_candles_dataset_id", "research_candles", ["dataset_id"])
    op.create_index("ix_research_candles_dataset_time", "research_candles", ["dataset_id", "timestamp"])
    op.create_table(
        "research_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("run_name", sa.String(120)),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id"), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("git_commit", sa.String(64)),
        sa.Column("git_dirty", sa.Boolean()),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("split_definition", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_research_run_id"),
    )
    op.create_index("ix_research_runs_run_id", "research_runs", ["run_id"])
    op.create_index("ix_research_runs_strategy_id", "research_runs", ["strategy_id"])
    op.create_index("ix_research_runs_status", "research_runs", ["status"])
    op.create_index("ix_research_runs_created", "research_runs", ["created_at"])
    op.create_table(
        "research_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.id"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("market_regime", sa.String(32), nullable=False),
        sa.Column("entry_price", sa.Float()),
        sa.Column("stop_loss", sa.Float()),
        sa.Column("take_profit", sa.Float()),
        sa.Column("risk_reward_ratio", sa.Float()),
        sa.Column("confidence", sa.Float()),
        sa.Column("feature_context", sa.JSON(), nullable=False),
        sa.Column("rejection_stage", sa.String(64)),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_research_decisions_run_id", "research_decisions", ["run_id"])
    op.create_index("ix_research_decisions_run_time", "research_decisions", ["run_id", "timestamp"])
    op.create_index("ix_research_decisions_decision", "research_decisions", ["decision"])
    op.create_table(
        "research_outcomes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.id"), nullable=False),
        sa.Column("decision_id", sa.String(36), sa.ForeignKey("research_decisions.id"), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("terminal_status", sa.String(32), nullable=False),
        sa.Column("realized_r", sa.Float()),
        sa.Column("bars_held", sa.Integer(), nullable=False),
        sa.Column("mfe_r", sa.Float()),
        sa.Column("mae_r", sa.Float()),
        sa.Column("terminal_candle_timestamp", sa.DateTime()),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("decision_id", "policy_version", name="uq_research_outcome_policy"),
    )
    op.create_index("ix_research_outcomes_run_id", "research_outcomes", ["run_id"])
    op.create_index("ix_research_outcomes_decision_id", "research_outcomes", ["decision_id"])
    op.create_index("ix_research_outcomes_terminal_status", "research_outcomes", ["terminal_status"])
    op.create_table(
        "research_metrics",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.id"), nullable=False),
        sa.Column("metric_key", sa.String(100), nullable=False),
        sa.Column("value", sa.Float()),
        sa.Column("denominator", sa.Integer()),
        sa.Column("dimension_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", "metric_key", name="uq_research_metric"),
    )
    op.create_index("ix_research_metrics_run_id", "research_metrics", ["run_id"])


def downgrade() -> None:
    op.drop_table("research_metrics")
    op.drop_table("research_outcomes")
    op.drop_table("research_decisions")
    op.drop_table("research_runs")
    op.drop_table("research_candles")
    op.drop_table("research_datasets")
