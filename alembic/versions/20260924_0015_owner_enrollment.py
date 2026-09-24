"""Add single-owner invariant and one-time account enrollment records."""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0015"
down_revision = "20260924_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_auth_users_single_owner",
        "auth_users",
        ["role"],
        unique=True,
        sqlite_where=sa.text("role = 'OWNER'"),
        postgresql_where=sa.text("role = 'OWNER'"),
    )
    op.add_column(
        "auth_audit_events",
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_auth_audit_events_actor_user_id", "auth_audit_events", ["actor_user_id"])
    op.create_table(
        "auth_enrollments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("auth_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("auth_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.UniqueConstraint("token_hash", name="uq_auth_enrollments_token_hash"),
    )
    op.create_index("ix_auth_enrollments_user_id", "auth_enrollments", ["user_id"])
    op.create_index("ix_auth_enrollments_expires_at", "auth_enrollments", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_enrollments_expires_at", table_name="auth_enrollments")
    op.drop_index("ix_auth_enrollments_user_id", table_name="auth_enrollments")
    op.drop_table("auth_enrollments")
    op.drop_index("ix_auth_audit_events_actor_user_id", table_name="auth_audit_events")
    op.drop_column("auth_audit_events", "actor_user_id")
    op.drop_index("uq_auth_users_single_owner", table_name="auth_users")
