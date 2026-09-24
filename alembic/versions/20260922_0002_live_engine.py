"""Add the frozen Phase 1.6 live-engine cursor and deal tables."""

import sqlalchemy as sa

from alembic import op

revision = "20260922_0002"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candle_cursors",
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("last_completed_timestamp", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.UniqueConstraint("symbol", "timeframe", name="uq_candle_cursor"),
    )
    op.create_index("ix_candle_cursors_symbol", "candle_cursors", ["symbol"])
    op.create_index("ix_candle_cursors_timeframe", "candle_cursors", ["timeframe"])

    op.create_table(
        "broker_deals",
        sa.Column("deal_ticket", sa.BigInteger(), nullable=False),
        sa.Column("order_ticket", sa.BigInteger()),
        sa.Column("position_id", sa.BigInteger()),
        sa.Column("symbol", sa.String(64), nullable=False, index=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("deal_type", sa.String(32)),
        sa.Column("entry_type", sa.String(32)),
        sa.Column("volume", sa.Float()),
        sa.Column("price", sa.Float()),
        sa.Column("profit", sa.Float()),
        sa.Column("commission", sa.Float()),
        sa.Column("swap", sa.Float()),
        sa.Column("fee", sa.Float()),
        sa.Column("comment", sa.Text()),
        sa.Column("magic_number", sa.BigInteger()),
        sa.Column("reason", sa.String(64)),
        sa.Column("raw_payload", sa.JSON()),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.UniqueConstraint("deal_ticket", name="uq_broker_deal_ticket"),
    )
    op.create_index(
        "ix_broker_deal_position_time", "broker_deals", ["position_id", "timestamp"]
    )
    op.create_index("ix_broker_deal_symbol_time", "broker_deals", ["symbol", "timestamp"])

    op.create_table(
        "history_cursors",
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("last_timestamp", sa.DateTime()),
        sa.Column("last_identifier", sa.BigInteger()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.UniqueConstraint("scope", name="uq_history_cursor_scope"),
    )


def downgrade() -> None:
    op.drop_table("history_cursors")
    op.drop_table("broker_deals")
    op.drop_table("candle_cursors")
