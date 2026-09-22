"""Record successful empty-position observations at snapshot level."""

import sqlalchemy as sa

from alembic import op

revision = "20260922_0004"
down_revision = "20260922_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("market_snapshots")
    }
    if "positions_observed_successfully" not in existing:
        op.add_column(
            "market_snapshots",
            sa.Column(
                "positions_observed_successfully",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "open_position_count" not in existing:
        op.add_column(
            "market_snapshots",
            sa.Column(
                "open_position_count", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
        )


def downgrade() -> None:
    op.drop_column("market_snapshots", "open_position_count")
    op.drop_column("market_snapshots", "positions_observed_successfully")
