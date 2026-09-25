"""Index latest health observations by component and timestamp."""

import sqlalchemy as sa

from alembic import op

revision = "20260925_0019"
down_revision = "20260925_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_system_health_component_timestamp",
        "system_health",
        ["component", sa.text("timestamp DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_system_health_component_timestamp",
        table_name="system_health",
    )
