"""Add durable shadow outcome evaluations."""
# ruff: noqa: E501

import sqlalchemy as sa

from alembic import op

revision = "20260922_0006"
down_revision = "20260922_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "shadow_outcomes" in inspector.get_table_names():
        return
    op.create_table(
        "shadow_outcomes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("evaluation_policy_version", sa.String(length=64), nullable=False),
        sa.Column("decision_m5_timestamp", sa.DateTime(), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("take_profit", sa.Float(), nullable=True),
        sa.Column("initial_risk_distance", sa.Float(), nullable=True),
        sa.Column("target_r_multiple", sa.Float(), nullable=True),
        sa.Column("evaluation_started_at", sa.DateTime(), nullable=False),
        sa.Column("terminal_candle_timestamp", sa.DateTime(), nullable=True),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("realized_r", sa.Float(), nullable=True),
        sa.Column("bars_held", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_favorable_excursion_price", sa.Float(), nullable=True),
        sa.Column("max_adverse_excursion_price", sa.Float(), nullable=True),
        sa.Column("mfe_r", sa.Float(), nullable=True),
        sa.Column("mae_r", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["decision_id"], ["shadow_decisions.id"]),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["market_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_id", "evaluation_policy_version", name="uq_shadow_outcome_policy"
        ),
    )
    op.create_index("ix_shadow_outcomes_decision_id", "shadow_outcomes", ["decision_id"])
    op.create_index("ix_shadow_outcomes_terminal_time", "shadow_outcomes", ["terminal_candle_timestamp"])
    op.create_index("ix_shadow_outcomes_status", "shadow_outcomes", ["terminal_status"])


def downgrade() -> None:
    op.drop_index("ix_shadow_outcomes_status", table_name="shadow_outcomes")
    op.drop_index("ix_shadow_outcomes_terminal_time", table_name="shadow_outcomes")
    op.drop_index("ix_shadow_outcomes_decision_id", table_name="shadow_outcomes")
    op.drop_table("shadow_outcomes")
