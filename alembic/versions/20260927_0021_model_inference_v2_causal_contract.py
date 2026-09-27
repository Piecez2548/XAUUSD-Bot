"""Add nullable future-only causal provenance fields.

The columns are nullable so existing Strategy Intelligence and Forward Shadow
rows retain their historical V1 semantics.  No historical value is inferred
or backfilled by this migration.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260927_0021"
down_revision = "20260926_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_intelligence_records",
        sa.Column("provenance_contract_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "strategy_intelligence_records",
        sa.Column("source_timeframe", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "strategy_intelligence_records",
        sa.Column("confirmation_timeframe", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "strategy_intelligence_records",
        sa.Column("observation_available_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "strategy_intelligence_records",
        sa.Column("pair_zone_decision_evidence_id", sa.String(length=128), nullable=True),
    )

    op.add_column(
        "forward_validation_signals",
        sa.Column("provenance_contract_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("source_timeframe", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("confirmation_timeframe", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("signal_decision_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("decision_available_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("symbol", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "forward_validation_signals",
        sa.Column("pair_zone_decision_evidence_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "forward_validation_trades",
        sa.Column("outcome_available_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_forward_signal_pair_zone_decision_evidence",
        "forward_validation_signals",
        ["pair_zone_decision_evidence_id"],
        unique=True,
    )
    bind = op.get_bind()
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
        op.execute(
            """
            CREATE TRIGGER guard_v2_intelligence_provenance_update
            BEFORE UPDATE OF provenance_contract_version, source_timeframe,
                confirmation_timeframe, observation_available_at, pair_zone_event_id,
                pair_zone_decision_evidence_id
            ON strategy_intelligence_records
            WHEN OLD.provenance_contract_version = 'strategy_intelligence_provenance_v2'
                AND (
                    OLD.provenance_contract_version IS NOT NEW.provenance_contract_version
                    OR OLD.source_timeframe IS NOT NEW.source_timeframe
                    OR OLD.confirmation_timeframe IS NOT NEW.confirmation_timeframe
                    OR OLD.observation_available_at IS NOT NEW.observation_available_at
                    OR OLD.pair_zone_event_id IS NOT NEW.pair_zone_event_id
                    OR OLD.pair_zone_decision_evidence_id IS NOT NEW.pair_zone_decision_evidence_id
                )
            BEGIN
                SELECT RAISE(ABORT, 'immutable V2 intelligence provenance');
            END
            """
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_v2_forward_signal_provenance_update()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.provenance_contract_version = 'strategy_intelligence_provenance_v2'
                    AND ROW(OLD.signal_id, OLD.session_id, OLD.timestamp, OLD.symbol,
                        OLD.decision, OLD.zone_id, OLD.entry_price, OLD.stop_loss,
                        OLD.risk_distance, OLD.rr, OLD.take_profit,
                        OLD.pair_first_timestamp, OLD.pair_second_timestamp,
                        OLD.h1_context_json, OLD.confirmation_candle_json,
                        OLD.market_observation_json, OLD.strategy_hash,
                        OLD.provenance_contract_version, OLD.source_timeframe,
                        OLD.confirmation_timeframe, OLD.signal_decision_at,
                        OLD.decision_available_at, OLD.pair_zone_decision_evidence_id)
                    IS DISTINCT FROM ROW(NEW.signal_id, NEW.session_id, NEW.timestamp,
                        NEW.symbol, NEW.decision, NEW.zone_id, NEW.entry_price,
                        NEW.stop_loss, NEW.risk_distance, NEW.rr, NEW.take_profit,
                        NEW.pair_first_timestamp, NEW.pair_second_timestamp,
                        NEW.h1_context_json, NEW.confirmation_candle_json,
                        NEW.market_observation_json, NEW.strategy_hash,
                        NEW.provenance_contract_version, NEW.source_timeframe,
                        NEW.confirmation_timeframe, NEW.signal_decision_at,
                        NEW.decision_available_at, NEW.pair_zone_decision_evidence_id)
                THEN RAISE EXCEPTION 'immutable V2 forward signal provenance';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER guard_v2_forward_signal_provenance_update
            BEFORE UPDATE ON forward_validation_signals
            FOR EACH ROW EXECUTE FUNCTION guard_v2_forward_signal_provenance_update();
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_v2_intelligence_provenance_update()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.provenance_contract_version = 'strategy_intelligence_provenance_v2'
                    AND ROW(OLD.provenance_contract_version, OLD.source_timeframe,
                        OLD.confirmation_timeframe, OLD.observation_available_at,
                        OLD.pair_zone_event_id, OLD.pair_zone_decision_evidence_id)
                    IS DISTINCT FROM ROW(NEW.provenance_contract_version,
                        NEW.source_timeframe, NEW.confirmation_timeframe,
                        NEW.observation_available_at, NEW.pair_zone_event_id,
                        NEW.pair_zone_decision_evidence_id)
                THEN RAISE EXCEPTION 'immutable V2 intelligence provenance';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER guard_v2_intelligence_provenance_update
            BEFORE UPDATE ON strategy_intelligence_records
            FOR EACH ROW EXECUTE FUNCTION guard_v2_intelligence_provenance_update();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER guard_v2_forward_signal_provenance_update")
        op.execute("DROP TRIGGER guard_v2_intelligence_provenance_update")
    elif bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER guard_v2_forward_signal_provenance_update "
            "ON forward_validation_signals"
        )
        op.execute(
            "DROP TRIGGER guard_v2_intelligence_provenance_update "
            "ON strategy_intelligence_records"
        )
        op.execute("DROP FUNCTION guard_v2_forward_signal_provenance_update()")
        op.execute("DROP FUNCTION guard_v2_intelligence_provenance_update()")
    op.drop_index(
        "uq_forward_signal_pair_zone_decision_evidence",
        table_name="forward_validation_signals",
    )
    for name in (
        "pair_zone_decision_evidence_id",
        "symbol",
        "decision_available_at",
        "signal_decision_at",
        "confirmation_timeframe",
        "source_timeframe",
        "provenance_contract_version",
    ):
        op.drop_column("forward_validation_signals", name)
    for name in (
        "outcome_available_at",
    ):
        op.drop_column("forward_validation_trades", name)
    for name in (
        "pair_zone_decision_evidence_id",
        "observation_available_at",
        "confirmation_timeframe",
        "source_timeframe",
        "provenance_contract_version",
    ):
        op.drop_column("strategy_intelligence_records", name)
