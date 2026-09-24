"""Add fail-closed Demo execution control and idempotency records."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "20260924_0013"
down_revision = "20260924_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_execution_controls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("control_key", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("control_key", name="uq_demo_execution_control_key"),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    op.bulk_insert(
        sa.table(
            "demo_execution_controls",
            sa.column("id", sa.String(36)),
            sa.column("control_key", sa.String(32)),
            sa.column("enabled", sa.Boolean()),
            sa.column("reason", sa.String(128)),
            sa.column("updated_by", sa.String(64)),
            sa.column("created_at", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {
                "id": str(uuid4()),
                "control_key": "DEMO",
                "enabled": False,
                "reason": "DEFAULT_KILL_SWITCH",
                "updated_by": "migration",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.create_table(
        "demo_execution_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forward_signal_id",
            sa.String(36),
            sa.ForeignKey("forward_validation_signals.id"),
            nullable=False,
        ),
        sa.Column(
            "forward_session_id",
            sa.String(36),
            sa.ForeignKey("forward_validation_sessions.id"),
            nullable=False,
        ),
        sa.Column("intelligence_candidate_id", sa.String(100), nullable=False),
        sa.Column("pair_zone_event_id", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("rejection_reason", sa.String(128)),
        sa.Column("gate_reasons_json", sa.JSON(), nullable=False),
        sa.Column("planned_entry", sa.Float()),
        sa.Column("submitted_entry", sa.Float()),
        sa.Column("stop_loss", sa.Float()),
        sa.Column("take_profit", sa.Float()),
        sa.Column("volume", sa.Float()),
        sa.Column("risk_percent", sa.Float()),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("broker_retcode", sa.Integer()),
        sa.Column("broker_order_ticket", sa.BigInteger()),
        sa.Column("broker_deal_ticket", sa.BigInteger()),
        sa.Column("broker_position_ticket", sa.BigInteger()),
        sa.Column("broker_comment", sa.String(255)),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("acknowledged_at", sa.DateTime()),
        sa.Column("terminal_outcome_at", sa.DateTime()),
        sa.Column("terminal_status", sa.String(32)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("forward_signal_id", name="uq_demo_execution_forward_signal"),
    )
    op.create_index(
        "ix_demo_execution_status_created",
        "demo_execution_records",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_demo_execution_position",
        "demo_execution_records",
        ["broker_position_ticket"],
    )
    op.create_index(
        "ix_demo_execution_records_intelligence_candidate_id",
        "demo_execution_records",
        ["intelligence_candidate_id"],
    )
    op.create_index(
        "ix_demo_execution_records_pair_zone_event_id",
        "demo_execution_records",
        ["pair_zone_event_id"],
    )
    op.create_index(
        "ix_demo_execution_records_symbol",
        "demo_execution_records",
        ["symbol"],
    )
    op.create_index(
        "ix_demo_execution_records_status",
        "demo_execution_records",
        ["status"],
    )
    op.create_index(
        "ix_demo_execution_records_broker_order_ticket",
        "demo_execution_records",
        ["broker_order_ticket"],
    )
    op.create_index(
        "ix_demo_execution_records_broker_deal_ticket",
        "demo_execution_records",
        ["broker_deal_ticket"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_demo_execution_records_broker_deal_ticket", table_name="demo_execution_records"
    )
    op.drop_index(
        "ix_demo_execution_records_broker_order_ticket", table_name="demo_execution_records"
    )
    op.drop_index("ix_demo_execution_records_status", table_name="demo_execution_records")
    op.drop_index("ix_demo_execution_records_symbol", table_name="demo_execution_records")
    op.drop_index(
        "ix_demo_execution_records_pair_zone_event_id", table_name="demo_execution_records"
    )
    op.drop_index(
        "ix_demo_execution_records_intelligence_candidate_id", table_name="demo_execution_records"
    )
    op.drop_index("ix_demo_execution_position", table_name="demo_execution_records")
    op.drop_index("ix_demo_execution_status_created", table_name="demo_execution_records")
    op.drop_table("demo_execution_records")
    op.drop_table("demo_execution_controls")
