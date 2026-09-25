"""Index risk snapshots by their market snapshot association."""

from alembic import op

revision = "20260925_0017"
down_revision = "20260924_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_risk_snapshots_market_snapshot_id",
        "risk_snapshots",
        ["market_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_risk_snapshots_market_snapshot_id",
        table_name="risk_snapshots",
    )
