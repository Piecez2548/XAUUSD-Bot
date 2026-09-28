"""Add the independent Momentum Breakout V1 canonical setup contract."""

import sqlalchemy as sa

from alembic import op

revision = "20260928_0022"
down_revision = "20260927_0021"
branch_labels = None
depends_on = None

_PAIR_DEFAULT = "'PAIR_ZONE_REJECTION'"


def upgrade() -> None:
    op.add_column(
        "forward_validation_signals",
        sa.Column(
            "setup_type",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text(_PAIR_DEFAULT),
        ),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("setup_event_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column(
            "setup_provenance_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "demo_execution_records",
        sa.Column(
            "setup_type",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text(_PAIR_DEFAULT),
        ),
    )
    op.add_column(
        "demo_execution_records",
        sa.Column("setup_event_id", sa.String(length=128), nullable=True),
    )

    with op.batch_alter_table("forward_validation_signals", schema=None) as batch:
        batch.alter_column("zone_id", existing_type=sa.String(length=100), nullable=True)
        batch.drop_constraint("uq_forward_signal_candle", type_="unique")
        batch.create_unique_constraint(
            "uq_forward_signal_setup_candle", ["session_id", "timestamp", "setup_type"]
        )

    op.create_index(
        "ix_forward_validation_signals_setup_event_id",
        "forward_validation_signals",
        ["setup_event_id"],
    )
    op.create_index(
        "ix_demo_execution_records_setup_event_id",
        "demo_execution_records",
        ["setup_event_id"],
    )
    op.create_index(
        "uq_forward_signal_setup_event",
        "forward_validation_signals",
        ["setup_event_id"],
        unique=True,
    )

    # Batch recreation of the SQLite signal table removes triggers that are
    # not represented in SQLAlchemy metadata. Reinstall the existing V2
    # immutability guard, extended to cover the new setup identity fields.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER guard_v2_forward_signal_provenance_update
            BEFORE UPDATE ON forward_validation_signals
            WHEN OLD.provenance_contract_version = 'strategy_intelligence_provenance_v2'
                AND (
                    OLD.signal_id IS NOT NEW.signal_id OR OLD.session_id IS NOT NEW.session_id
                    OR OLD.timestamp IS NOT NEW.timestamp OR OLD.symbol IS NOT NEW.symbol
                    OR OLD.decision IS NOT NEW.decision OR OLD.zone_id IS NOT NEW.zone_id
                    OR OLD.setup_type IS NOT NEW.setup_type
                    OR OLD.setup_event_id IS NOT NEW.setup_event_id
                    OR OLD.setup_provenance_json IS NOT NEW.setup_provenance_json
                    OR OLD.entry_price IS NOT NEW.entry_price OR OLD.stop_loss IS NOT NEW.stop_loss
                    OR OLD.risk_distance IS NOT NEW.risk_distance OR OLD.rr IS NOT NEW.rr
                    OR OLD.take_profit IS NOT NEW.take_profit
                    OR OLD.pair_first_timestamp IS NOT NEW.pair_first_timestamp
                    OR OLD.pair_second_timestamp IS NOT NEW.pair_second_timestamp
                    OR OLD.h1_context_json IS NOT NEW.h1_context_json
                    OR OLD.confirmation_candle_json IS NOT NEW.confirmation_candle_json
                    OR OLD.market_observation_json IS NOT NEW.market_observation_json
                    OR OLD.strategy_hash IS NOT NEW.strategy_hash
                    OR OLD.provenance_contract_version IS NOT NEW.provenance_contract_version
                    OR OLD.source_timeframe IS NOT NEW.source_timeframe
                    OR OLD.confirmation_timeframe IS NOT NEW.confirmation_timeframe
                    OR OLD.signal_decision_at IS NOT NEW.signal_decision_at
                    OR OLD.decision_available_at IS NOT NEW.decision_available_at
                    OR OLD.pair_zone_decision_evidence_id IS NOT NEW.pair_zone_decision_evidence_id
                )
            BEGIN
                SELECT RAISE(ABORT, 'immutable V2 forward signal provenance');
            END
            """
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_momentum_setup_update()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.provenance_contract_version = 'strategy_intelligence_provenance_v2'
                    AND ROW(OLD.setup_type, OLD.setup_event_id, OLD.setup_provenance_json)
                    IS DISTINCT FROM ROW(NEW.setup_type, NEW.setup_event_id,
                        NEW.setup_provenance_json)
                THEN RAISE EXCEPTION 'immutable Momentum setup provenance';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER guard_momentum_setup_update
            BEFORE UPDATE ON forward_validation_signals
            FOR EACH ROW EXECUTE FUNCTION guard_momentum_setup_update();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS guard_v2_forward_signal_provenance_update")
    elif bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS guard_momentum_setup_update "
            "ON forward_validation_signals"
        )
        op.execute("DROP FUNCTION IF EXISTS guard_momentum_setup_update()")
    op.drop_index("ix_demo_execution_records_setup_event_id", table_name="demo_execution_records")
    op.drop_index("uq_forward_signal_setup_event", table_name="forward_validation_signals")
    op.drop_index(
        "ix_forward_validation_signals_setup_event_id",
        table_name="forward_validation_signals",
    )
    with op.batch_alter_table("forward_validation_signals", schema=None) as batch:
        batch.drop_constraint("uq_forward_signal_setup_candle", type_="unique")
        batch.create_unique_constraint(
            "uq_forward_signal_candle", ["session_id", "timestamp"]
        )
        batch.alter_column("zone_id", existing_type=sa.String(length=100), nullable=False)
        batch.drop_column("setup_provenance_json")
        batch.drop_column("setup_event_id")
        batch.drop_column("setup_type")
    with op.batch_alter_table("demo_execution_records", schema=None) as batch:
        batch.drop_column("setup_event_id")
        batch.drop_column("setup_type")
    if bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER guard_v2_forward_signal_provenance_update
            BEFORE UPDATE OF signal_id, session_id, timestamp, symbol, decision, zone_id,
                entry_price, stop_loss, risk_distance, rr, take_profit,
                pair_first_timestamp, pair_second_timestamp, h1_context_json,
                confirmation_candle_json, market_observation_json, strategy_hash,
                provenance_contract_version, source_timeframe, confirmation_timeframe,
                signal_decision_at, decision_available_at, pair_zone_decision_evidence_id
            ON forward_validation_signals
            WHEN OLD.provenance_contract_version = 'strategy_intelligence_provenance_v2'
                AND (
                    OLD.signal_id IS NOT NEW.signal_id OR OLD.session_id IS NOT NEW.session_id
                    OR OLD.timestamp IS NOT NEW.timestamp OR OLD.symbol IS NOT NEW.symbol
                    OR OLD.decision IS NOT NEW.decision OR OLD.zone_id IS NOT NEW.zone_id
                    OR OLD.entry_price IS NOT NEW.entry_price OR OLD.stop_loss IS NOT NEW.stop_loss
                    OR OLD.risk_distance IS NOT NEW.risk_distance OR OLD.rr IS NOT NEW.rr
                    OR OLD.take_profit IS NOT NEW.take_profit
                    OR OLD.pair_first_timestamp IS NOT NEW.pair_first_timestamp
                    OR OLD.pair_second_timestamp IS NOT NEW.pair_second_timestamp
                    OR OLD.h1_context_json IS NOT NEW.h1_context_json
                    OR OLD.confirmation_candle_json IS NOT NEW.confirmation_candle_json
                    OR OLD.market_observation_json IS NOT NEW.market_observation_json
                    OR OLD.strategy_hash IS NOT NEW.strategy_hash
                    OR OLD.provenance_contract_version IS NOT NEW.provenance_contract_version
                    OR OLD.source_timeframe IS NOT NEW.source_timeframe
                    OR OLD.confirmation_timeframe IS NOT NEW.confirmation_timeframe
                    OR OLD.signal_decision_at IS NOT NEW.signal_decision_at
                    OR OLD.decision_available_at IS NOT NEW.decision_available_at
                    OR OLD.pair_zone_decision_evidence_id IS NOT NEW.pair_zone_decision_evidence_id
                )
            BEGIN
                SELECT RAISE(ABORT, 'immutable V2 forward signal provenance');
            END
            """
        )
