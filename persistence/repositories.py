"""Transactional persistence services for snapshots, events, and query views."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from domain.events import DomainEvent
from models.history import DealFact
from models.market import AccountState, Candle, MarketSnapshot, SymbolSpecification, Timeframe
from models.observatory import PersistenceResult, RiskSnapshot
from models.shadow import ShadowDecision
from persistence.database import Database
from persistence.orm import (
    AccountRecord,
    AccountSnapshotRecord,
    BrokerDealRecord,
    CandleCursorRecord,
    CandleRecord,
    ControlAuditRecord,
    HistoryCursorRecord,
    MarketSnapshotRecord,
    PositionRecord,
    PositionSnapshotRecord,
    RiskSnapshotRecord,
    ShadowDecisionRecord,
    SymbolRecord,
    SystemEventRecord,
    SystemHealthRecord,
)


def _candle_dict(candle: Candle) -> dict[str, object]:
    return {
        "timestamp": candle.timestamp.isoformat(),
        "raw_timestamp": candle.raw_timestamp,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "tick_volume": candle.tick_volume,
        "spread": candle.spread,
        "real_volume": candle.real_volume,
    }


class SnapshotRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def persist(self, snapshot: MarketSnapshot, risk: RiskSnapshot) -> PersistenceResult:
        with self._database.session() as session:
            account = self._get_or_create_account(session, snapshot)
            symbol = self._get_or_create_symbol(session, snapshot)
            account_snapshot = AccountSnapshotRecord(
                account_id=account.id,
                timestamp=snapshot.generated_at,
                balance=snapshot.account.balance,
                equity=snapshot.account.equity,
                margin=snapshot.account.margin,
                free_margin=snapshot.account.free_margin,
                margin_level=snapshot.account.margin_level,
                profit=snapshot.account.profit,
                leverage=snapshot.account.leverage,
            )
            session.add(account_snapshot)
            session.flush()

            windows: dict[str, dict[str, object]] = {}
            latest_candles: dict[str, dict[str, object]] = {}
            for timeframe, candles in snapshot.candles.items():
                closed = candles[:-1]
                self._persist_closed_candles(session, symbol.id, timeframe, closed)
                windows[timeframe.value] = {
                    "first": closed[0].timestamp.isoformat() if closed else None,
                    "last": closed[-1].timestamp.isoformat() if closed else None,
                    "closed_count": len(closed),
                    "requested_count": len(candles),
                }
                latest_candles[timeframe.value] = _candle_dict(candles[-1])

            market_record = MarketSnapshotRecord(
                symbol_id=symbol.id,
                account_snapshot_id=account_snapshot.id,
                timestamp=snapshot.generated_at,
                bid=snapshot.tick.bid,
                ask=snapshot.tick.ask,
                spread=snapshot.symbol.spread,
                positions_observed_successfully=True,
                open_position_count=len(snapshot.positions),
                candle_windows=windows,
                latest_candles=latest_candles,
                technical_features=None,
                market_regime=None,
                session=None,
                relevant_news_ids=None,
            )
            session.add(market_record)
            session.flush()
            self._persist_positions(session, snapshot, symbol.id, market_record.id)

            risk_record = RiskSnapshotRecord(
                market_snapshot_id=market_record.id,
                timestamp=risk.timestamp,
                equity=risk.equity,
                balance=risk.balance,
                open_risk_percent=risk.open_risk_percent,
                open_risk_amount=risk.open_risk_amount,
                remaining_risk_percent=risk.remaining_risk_percent,
                risk_per_position=[item.model_dump(mode="json") for item in risk.risk_per_position],
                daily_pnl=risk.daily_pnl,
                daily_realized_loss=risk.daily_realized_loss,
                drawdown_percent=risk.drawdown_percent,
                max_trade_risk_percent=risk.max_trade_risk_percent,
                max_aggregate_risk_percent=risk.max_aggregate_risk_percent,
                open_positions_count=risk.open_positions_count,
                unbounded_positions_count=risk.unbounded_positions_count,
                margin_usage_percent=risk.margin_usage_percent,
                free_margin=risk.free_margin,
            )
            session.add(risk_record)
            session.flush()
            return PersistenceResult(
                market_snapshot_id=UUID(market_record.id),
                account_snapshot_id=UUID(account_snapshot.id),
                risk_snapshot_id=UUID(risk_record.id),
            )

    def persist_live_account(self, account: AccountState, timestamp) -> str:
        with self._database.session() as session:
            record = session.scalar(
                select(AccountRecord).where(
                    AccountRecord.server == account.server,
                    AccountRecord.currency == account.currency,
                    AccountRecord.trade_mode == account.trade_mode,
                )
            )
            if record is None:
                record = AccountRecord(
                    server=account.server,
                    currency=account.currency,
                    trade_mode=account.trade_mode,
                )
                session.add(record)
                session.flush()
            snapshot = AccountSnapshotRecord(
                account_id=record.id,
                timestamp=timestamp,
                balance=account.balance,
                equity=account.equity,
                margin=account.margin,
                free_margin=account.free_margin,
                margin_level=account.margin_level,
                profit=account.profit,
                leverage=account.leverage,
            )
            session.add(snapshot)
            session.flush()
            return snapshot.id

    def persist_live_risk(self, risk: RiskSnapshot) -> str:
        with self._database.session() as session:
            record = RiskSnapshotRecord(
                market_snapshot_id=None,
                timestamp=risk.timestamp,
                equity=risk.equity,
                balance=risk.balance,
                open_risk_percent=risk.open_risk_percent,
                open_risk_amount=risk.open_risk_amount,
                remaining_risk_percent=risk.remaining_risk_percent,
                risk_per_position=[item.model_dump(mode="json") for item in risk.risk_per_position],
                daily_pnl=risk.daily_pnl,
                daily_realized_loss=risk.daily_realized_loss,
                drawdown_percent=risk.drawdown_percent,
                max_trade_risk_percent=risk.max_trade_risk_percent,
                max_aggregate_risk_percent=risk.max_aggregate_risk_percent,
                open_positions_count=risk.open_positions_count,
                unbounded_positions_count=risk.unbounded_positions_count,
                margin_usage_percent=risk.margin_usage_percent,
                free_margin=risk.free_margin,
            )
            session.add(record)
            session.flush()
            return record.id

    def persist_candle(
        self,
        symbol: SymbolSpecification,
        timeframe: Timeframe,
        candle: Candle,
    ) -> str:
        with self._database.session() as session:
            symbol_record = self._symbol_record_from_spec(session, symbol)
            existing = session.scalar(
                select(CandleRecord).where(
                    CandleRecord.symbol_id == symbol_record.id,
                    CandleRecord.timeframe == timeframe.value,
                    CandleRecord.timestamp == candle.timestamp,
                )
            )
            if existing is not None:
                return existing.id
            record = CandleRecord(
                symbol_id=symbol_record.id,
                timeframe=timeframe.value,
                timestamp=candle.timestamp,
                raw_timestamp=candle.raw_timestamp,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                tick_volume=candle.tick_volume,
                spread=candle.spread,
                real_volume=candle.real_volume,
            )
            session.add(record)
            session.flush()
            return record.id

    @staticmethod
    def _symbol_record_from_spec(session: Session, symbol: SymbolSpecification) -> SymbolRecord:
        record = session.scalar(select(SymbolRecord).where(SymbolRecord.name == symbol.name))
        values = {
            "digits": symbol.digits,
            "point": symbol.point,
            "trade_tick_size": symbol.trade_tick_size,
            "trade_tick_value": symbol.trade_tick_value,
            "trade_tick_value_profit": symbol.trade_tick_value_profit,
            "trade_tick_value_loss": symbol.trade_tick_value_loss,
            "contract_size": symbol.contract_size,
            "volume_min": symbol.volume_min,
            "volume_max": symbol.volume_max,
            "volume_step": symbol.volume_step,
            "trade_mode": symbol.trade_mode,
            "trade_mode_name": symbol.trade_mode_name,
        }
        if record is None:
            record = SymbolRecord(name=symbol.name, **values)
            session.add(record)
            session.flush()
        return record

    @staticmethod
    def _get_or_create_account(session: Session, snapshot: MarketSnapshot) -> AccountRecord:
        query = select(AccountRecord).where(
            AccountRecord.server == snapshot.account.server,
            AccountRecord.currency == snapshot.account.currency,
            AccountRecord.trade_mode == snapshot.account.trade_mode,
        )
        account = session.scalar(query)
        if account is None:
            account = AccountRecord(
                server=snapshot.account.server,
                currency=snapshot.account.currency,
                trade_mode=snapshot.account.trade_mode,
            )
            session.add(account)
            session.flush()
        return account

    @staticmethod
    def _get_or_create_symbol(session: Session, snapshot: MarketSnapshot) -> SymbolRecord:
        symbol = session.scalar(
            select(SymbolRecord).where(SymbolRecord.name == snapshot.symbol.name)
        )
        values = {
            "digits": snapshot.symbol.digits,
            "point": snapshot.symbol.point,
            "trade_tick_size": snapshot.symbol.trade_tick_size,
            "trade_tick_value": snapshot.symbol.trade_tick_value,
            "trade_tick_value_profit": snapshot.symbol.trade_tick_value_profit,
            "trade_tick_value_loss": snapshot.symbol.trade_tick_value_loss,
            "contract_size": snapshot.symbol.contract_size,
            "volume_min": snapshot.symbol.volume_min,
            "volume_max": snapshot.symbol.volume_max,
            "volume_step": snapshot.symbol.volume_step,
            "trade_mode": snapshot.symbol.trade_mode,
            "trade_mode_name": snapshot.symbol.trade_mode_name,
        }
        if symbol is None:
            symbol = SymbolRecord(name=snapshot.symbol.name, **values)
            session.add(symbol)
            session.flush()
        else:
            for key, value in values.items():
                setattr(symbol, key, value)
        return symbol

    @staticmethod
    def _persist_closed_candles(
        session: Session,
        symbol_id: str,
        timeframe: Timeframe,
        candles: tuple[Candle, ...],
    ) -> None:
        if not candles:
            return
        timestamps = tuple(candle.timestamp for candle in candles)
        existing = set(
            session.scalars(
                select(CandleRecord.timestamp).where(
                    CandleRecord.symbol_id == symbol_id,
                    CandleRecord.timeframe == timeframe.value,
                    CandleRecord.timestamp.in_(timestamps),
                )
            )
        )
        for candle in candles:
            if candle.timestamp in existing:
                continue
            session.add(
                CandleRecord(
                    symbol_id=symbol_id,
                    timeframe=timeframe.value,
                    timestamp=candle.timestamp,
                    raw_timestamp=candle.raw_timestamp,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    tick_volume=candle.tick_volume,
                    spread=candle.spread,
                    real_volume=candle.real_volume,
                )
            )

    @staticmethod
    def _persist_positions(
        session: Session,
        snapshot: MarketSnapshot,
        symbol_id: str,
        market_snapshot_id: str,
    ) -> None:
        observed_tickets = {position.ticket for position in snapshot.positions}
        active = session.scalars(
            select(PositionRecord).where(
                PositionRecord.symbol_id == symbol_id,
                PositionRecord.closed_observed_at.is_(None),
            )
        ).all()
        records = {item.broker_ticket: item for item in active}
        for ticket, record in records.items():
            if ticket not in observed_tickets:
                record.closed_observed_at = snapshot.generated_at
                record.last_seen_at = snapshot.generated_at

        for position in snapshot.positions:
            record = records.get(position.ticket)
            if record is None:
                record = PositionRecord(
                    broker_ticket=position.ticket,
                    symbol_id=symbol_id,
                    direction=position.type_name,
                    first_seen_at=snapshot.generated_at,
                    last_seen_at=snapshot.generated_at,
                    broker_open_time=position.open_time,
                    closed_observed_at=None,
                    magic_number=position.magic_number,
                    comment=position.comment,
                )
                session.add(record)
                session.flush()
            else:
                record.last_seen_at = snapshot.generated_at
            session.add(
                PositionSnapshotRecord(
                    position_id=record.id,
                    market_snapshot_id=market_snapshot_id,
                    timestamp=snapshot.generated_at,
                    volume=position.volume,
                    open_price=position.open_price,
                    current_price=position.current_price,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    profit=position.profit,
                    swap=position.swap,
                )
            )


class ShadowDecisionRepository:
    """Append-only persistence with candle-level idempotency."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def persist(self, decision: ShadowDecision) -> ShadowDecisionRecord:
        with self._database.session() as session:
            existing = session.scalar(
                select(ShadowDecisionRecord).where(
                    ShadowDecisionRecord.symbol == decision.symbol,
                    ShadowDecisionRecord.m5_candle_timestamp == decision.m5_candle_timestamp,
                    ShadowDecisionRecord.strategy_version == decision.strategy_version,
                )
            )
            if existing is not None:
                return existing
            record = ShadowDecisionRecord(
                id=str(decision.decision_id),
                created_at=decision.created_at,
                market_snapshot_id=(
                    str(decision.market_snapshot_id) if decision.market_snapshot_id else None
                ),
                symbol=decision.symbol,
                m5_candle_timestamp=decision.m5_candle_timestamp,
                decision=decision.decision.value,
                market_regime=decision.market_regime.value,
                entry_price=decision.entry_price,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                risk_reward_ratio=decision.risk_reward_ratio,
                requested_risk_percent=decision.requested_risk_percent,
                approved_risk_percent=decision.approved_risk_percent,
                hypothetical_volume=decision.hypothetical_volume,
                confidence=decision.confidence,
                strategy_name=decision.strategy_name,
                strategy_version=decision.strategy_version,
                reason_codes=list(decision.reason_codes),
                human_readable_reason=decision.human_readable_reason,
                risk_gate_state=decision.risk_gate_state,
                data_freshness=decision.data_freshness,
                execution_allowed=False,
                feature_context=decision.feature_context,
                outcome_status=decision.outcome_status,
            )
            session.add(record)
            session.flush()
            return record

    def latest(self) -> ShadowDecisionRecord | None:
        with self._database.session() as session:
            return session.scalar(
                select(ShadowDecisionRecord)
                # Event time is authoritative.  Catch-up may persist an older
                # candle after a newer one, so insertion time is not a valid
                # definition of "latest".
                .order_by(
                    ShadowDecisionRecord.m5_candle_timestamp.desc(),
                    ShadowDecisionRecord.created_at.desc(),
                    ShadowDecisionRecord.id.desc(),
                )
                .limit(1)
            )

    def has_decision(self, symbol: str, timestamp, strategy_version: str) -> bool:
        with self._database.session() as session:
            return (
                session.scalar(
                    select(ShadowDecisionRecord.id)
                    .where(
                        ShadowDecisionRecord.symbol == symbol,
                        ShadowDecisionRecord.m5_candle_timestamp == timestamp,
                        ShadowDecisionRecord.strategy_version == strategy_version,
                    )
                    .limit(1)
                )
                is not None
            )

    def list(self, limit: int = 100) -> list[ShadowDecisionRecord]:
        with self._database.session() as session:
            return list(
                session.scalars(
                    select(ShadowDecisionRecord)
                    .order_by(
                        ShadowDecisionRecord.m5_candle_timestamp.desc(),
                        ShadowDecisionRecord.created_at.desc(),
                        ShadowDecisionRecord.id.desc(),
                    )
                    .limit(limit)
                )
            )

    def summary(self) -> dict[str, int]:
        with self._database.session() as session:
            rows = session.execute(
                select(ShadowDecisionRecord.decision, func.count()).group_by(
                    ShadowDecisionRecord.decision
                )
            ).all()
            total = sum(int(count) for _decision, count in rows)
            result = {"total": total, "BUY": 0, "SELL": 0, "NO_TRADE": 0}
            result.update({decision: int(count) for decision, count in rows})
            result["pending_outcomes"] = int(
                session.scalar(
                    select(func.count())
                    .select_from(ShadowDecisionRecord)
                    .where(ShadowDecisionRecord.outcome_status == "PENDING")
                )
                or 0
            )
            return result


class EventRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def handle(self, event: DomainEvent) -> None:
        with self._database.session() as session:
            if session.get(SystemEventRecord, str(event.event_id)) is not None:
                return
            session.add(
                SystemEventRecord(
                    event_id=str(event.event_id),
                    event_type=event.event_type.value,
                    timestamp=event.timestamp,
                    source=event.source,
                    severity=event.severity.value,
                    correlation_id=str(event.correlation_id) if event.correlation_id else None,
                    payload=event.payload.model_dump(mode="json"),
                    schema_version=event.schema_version,
                )
            )


class HistoryRepository:
    """Idempotent broker-deal facts and reconciliation cursor."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def persist_deals(self, facts: tuple[DealFact, ...], *, scope: str) -> int:
        if not facts:
            return 0
        with self._database.session() as session:
            inserted = 0
            for fact in facts:
                if session.scalar(
                    select(BrokerDealRecord).where(BrokerDealRecord.deal_ticket == fact.deal_ticket)
                ):
                    continue
                session.add(
                    BrokerDealRecord(
                        deal_ticket=fact.deal_ticket,
                        order_ticket=fact.order_ticket,
                        position_id=fact.position_id,
                        symbol=fact.symbol,
                        timestamp=fact.timestamp,
                        deal_type=fact.deal_type,
                        entry_type=fact.entry_type,
                        volume=fact.volume,
                        price=fact.price,
                        profit=fact.profit,
                        commission=fact.commission,
                        swap=fact.swap,
                        fee=fact.fee,
                        comment=fact.comment,
                        magic_number=fact.magic_number,
                        reason=fact.reason,
                        raw_payload=fact.raw_payload,
                    )
                )
                inserted += 1
            latest = max(facts, key=lambda item: (item.timestamp, item.deal_ticket))
            cursor = session.scalar(
                select(HistoryCursorRecord).where(HistoryCursorRecord.scope == scope)
            )
            if cursor is None:
                cursor = HistoryCursorRecord(scope=scope)
                session.add(cursor)
            cursor.last_timestamp = latest.timestamp
            cursor.last_identifier = latest.deal_ticket
            return inserted

    def cursor(self, scope: str):
        with self._database.session() as session:
            record = session.scalar(
                select(HistoryCursorRecord).where(HistoryCursorRecord.scope == scope)
            )
            return None if record is None else (record.last_timestamp, record.last_identifier)

    def candle_cursor(self, symbol: str, timeframe: str):
        with self._database.session() as session:
            record = session.scalar(
                select(CandleCursorRecord).where(
                    CandleCursorRecord.symbol == symbol,
                    CandleCursorRecord.timeframe == timeframe,
                )
            )
            return None if record is None else record.last_completed_timestamp

    def set_candle_cursor(self, symbol: str, timeframe: str, timestamp) -> None:
        with self._database.session() as session:
            record = session.scalar(
                select(CandleCursorRecord).where(
                    CandleCursorRecord.symbol == symbol,
                    CandleCursorRecord.timeframe == timeframe,
                )
            )
            if record is None:
                record = CandleCursorRecord(
                    symbol=symbol,
                    timeframe=timeframe,
                    last_completed_timestamp=timestamp,
                )
                session.add(record)
            else:
                record.last_completed_timestamp = timestamp


TelegramDeliveryOutcome = Literal[
    "success",
    "recoverable_failure",
    "unrecoverable_failure",
    "configuration_error",
    "disabled",
]


class SystemHealthRepository:
    """Append-only component-health records without credentials or provider payloads."""

    _TELEGRAM_MESSAGES: dict[TelegramDeliveryOutcome, str] = {
        "success": "Telegram API request succeeded",
        "recoverable_failure": "Telegram delivery failed temporarily",
        "unrecoverable_failure": "Telegram API request was rejected",
        "configuration_error": "Telegram configuration is incomplete",
        "disabled": "Telegram integration is disabled",
    }

    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def latest_records(session: Session) -> list[SystemHealthRecord]:
        """Return the newest persisted row for every component.

        A fixed global LIMIT is incorrect for an append-only health table:
        busy workers can push a quiet component out of that window.
        """

        latest = (
            select(
                SystemHealthRecord.id,
                func.row_number()
                .over(
                    partition_by=SystemHealthRecord.component,
                    order_by=(SystemHealthRecord.timestamp.desc(), SystemHealthRecord.id.desc()),
                )
                .label("health_rank"),
            )
            .subquery()
        )
        return list(
            session.scalars(
                select(SystemHealthRecord).join(
                    latest,
                    SystemHealthRecord.id == latest.c.id,
                ).where(latest.c.health_rank == 1)
            ).all()
        )

    def record(
        self,
        component: str,
        status: str,
        *,
        message: str | None = None,
        latency_ms: float | None = None,
        metadata: dict[str, object] | None = None,
    ) -> str:
        with self._database.session() as session:
            record = SystemHealthRecord(
                timestamp=datetime.now(UTC),
                component=component,
                status=status,
                latency_ms=latency_ms,
                message=message,
                metadata_json=metadata,
            )
            session.add(record)
            session.flush()
            return record.id

    def record_telegram_outcome(
        self,
        outcome: TelegramDeliveryOutcome,
        *,
        latency_ms: float | None = None,
    ) -> str:
        """Persist one sanitized outcome and return the derived public service state."""

        with self._database.session() as session:
            consecutive_failures = self._telegram_consecutive_failures(session)
            if outcome == "success":
                status = "CONNECTED"
                consecutive_failures = 0
            elif outcome == "disabled":
                status = "DISABLED"
                consecutive_failures = 0
            elif outcome in {"unrecoverable_failure", "configuration_error"}:
                status = "ERROR"
                consecutive_failures += 1
            else:
                consecutive_failures += 1
                status = "ERROR" if consecutive_failures >= 3 else "DEGRADED"

            session.add(
                SystemHealthRecord(
                    timestamp=datetime.now(UTC),
                    component="telegram",
                    status=status,
                    latency_ms=latency_ms,
                    message=self._TELEGRAM_MESSAGES[outcome],
                    metadata_json={
                        "outcome": outcome,
                        "consecutive_failures": consecutive_failures,
                    },
                )
            )
            return status

    @staticmethod
    def _telegram_consecutive_failures(session: Session) -> int:
        rows = session.scalars(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == "telegram")
            .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
            .limit(100)
        ).all()
        failures = 0
        for row in rows:
            if row.status in {"CONNECTED", "DISABLED"}:
                break
            failures += 1
        return failures


class ControlAuditRepository:
    """Persist only the identifiers and result needed to audit control actions."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(
        self,
        *,
        command: str,
        chat_id: str | None,
        user_id: str | None,
        authorized: bool,
        result: str,
        correlation_id: str,
    ) -> str:
        with self._database.session() as session:
            record = ControlAuditRecord(
                timestamp=datetime.now(UTC),
                command=command,
                chat_id=chat_id,
                user_id=user_id,
                authorized=authorized,
                result=result,
                correlation_id=correlation_id,
            )
            session.add(record)
            session.flush()
            return record.id
