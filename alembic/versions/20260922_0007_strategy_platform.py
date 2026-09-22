"""Add immutable strategy identity and activation audit."""
# ruff: noqa: E501

import sqlalchemy as sa

from alembic import op

revision = "20260922_0007"
down_revision = "20260922_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shadow_decisions", sa.Column("config_version", sa.String(64), nullable=True))
    op.add_column("shadow_decisions", sa.Column("config_hash", sa.String(64), nullable=True))
    op.create_table(
        "strategy_activations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("effective_from_m5", sa.DateTime(), nullable=False),
        sa.Column("previous_strategy", sa.String(100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_strategy_activations_effective", "strategy_activations", ["effective_from_m5"])


def downgrade() -> None:
    op.drop_index("ix_strategy_activations_effective", table_name="strategy_activations")
    op.drop_table("strategy_activations")
    op.drop_column("shadow_decisions", "config_hash")
    op.drop_column("shadow_decisions", "config_version")
