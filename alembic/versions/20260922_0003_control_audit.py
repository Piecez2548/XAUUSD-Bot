"""Add Telegram control audit records."""

from alembic import op
from persistence.orm import Base

revision = "20260922_0003"
down_revision = "20260922_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS control_audit")
