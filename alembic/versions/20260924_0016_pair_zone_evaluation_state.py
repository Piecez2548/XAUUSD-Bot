"""Persist the latest generation-scoped canonical Pair Zone evaluation."""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0016"
down_revision = "20260924_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pair_zone_evaluations",
        sa.Column(
            "forward_session_id",
            sa.String(length=36),
            sa.ForeignKey("forward_validation_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("runtime_generation_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_m5_timestamp", sa.DateTime(), nullable=True),
        sa.Column("evaluated_m15_timestamp", sa.DateTime(), nullable=True),
        sa.Column("strategy_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("zone_id", sa.String(length=100), nullable=True),
        sa.Column("zone_lower", sa.Float(), nullable=True),
        sa.Column("zone_upper", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_pair_zone_evaluations_generation",
        "pair_zone_evaluations",
        ["runtime_generation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pair_zone_evaluations_generation", table_name="pair_zone_evaluations"
    )
    op.drop_table("pair_zone_evaluations")
