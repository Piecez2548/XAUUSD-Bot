"""Add live-engine cursors and broker deal facts."""

from alembic import op
from persistence.orm import Base

revision = "20260922_0002"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("history_cursors", "broker_deals", "candle_cursors"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
