"""Additive Phase 3.5.2 application authentication core."""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0014"
down_revision = "20260924_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("normalized_login", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("bound_tailscale_login", sa.String(length=254), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("normalized_login", name="uq_auth_users_normalized_login"),
        sa.CheckConstraint("role IN ('OWNER', 'ADMIN')", name="ck_auth_users_role"),
        sa.CheckConstraint(
            "state IN ('PROVISIONED', 'PENDING_APPROVAL', 'ACTIVE', 'LOCKED', 'REVOKED')",
            name="ck_auth_users_state",
        ),
    )
    op.create_index(
        "ix_auth_users_bound_tailscale_login", "auth_users", ["bound_tailscale_login"]
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("auth_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_idle_expires_at", "auth_sessions", ["idle_expires_at"])
    op.create_index(
        "ix_auth_sessions_absolute_expires_at", "auth_sessions", ["absolute_expires_at"]
    )
    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "user_id", sa.String(length=36), sa.ForeignKey("auth_users.id", ondelete="SET NULL")
        ),
        sa.Column("normalized_login", sa.String(length=254)),
        sa.Column("tailscale_login", sa.String(length=254)),
        sa.Column("session_id", sa.String(length=36)),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=64)),
    )
    op.create_index("ix_auth_audit_events_timestamp", "auth_audit_events", ["timestamp"])
    op.create_index("ix_auth_audit_events_user_id", "auth_audit_events", ["user_id"])
    op.create_index("ix_auth_audit_events_event_type", "auth_audit_events", ["event_type"])
    op.create_index("ix_auth_audit_events_session_id", "auth_audit_events", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_audit_events_session_id", table_name="auth_audit_events")
    op.drop_index("ix_auth_audit_events_event_type", table_name="auth_audit_events")
    op.drop_index("ix_auth_audit_events_user_id", table_name="auth_audit_events")
    op.drop_index("ix_auth_audit_events_timestamp", table_name="auth_audit_events")
    op.drop_table("auth_audit_events")
    op.drop_index("ix_auth_sessions_absolute_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_idle_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_auth_users_bound_tailscale_login", table_name="auth_users")
    op.drop_table("auth_users")
