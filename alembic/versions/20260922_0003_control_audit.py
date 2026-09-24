"""Add the frozen Phase 1.7 Telegram control audit table."""

import sqlalchemy as sa

from alembic import op

revision = "20260922_0003"
down_revision = "20260922_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_audit",
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("command", sa.String(32), nullable=False, index=True),
        sa.Column("chat_id", sa.String(128), index=True),
        sa.Column("user_id", sa.String(128), index=True),
        sa.Column("authorized", sa.Boolean(), nullable=False),
        sa.Column("result", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False, index=True),
        sa.Column("id", sa.String(36), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("control_audit")
