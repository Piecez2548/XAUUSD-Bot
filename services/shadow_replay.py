"""Deterministic replay helper for completed snapshots (no optimization)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select

from models.market import (
    AccountState,
    Candle,
    MarketSnapshot,
    Position,
    SymbolSpecification,
    Tick,
    Timeframe,
)
from models.observatory import PositionRisk, RiskSnapshot
from models.shadow import ShadowAction
from persistence.orm import (
    AccountRecord,
    AccountSnapshotRecord,
    CandleRecord,
    MarketSnapshotRecord,
    PositionRecord,
    PositionSnapshotRecord,
    RiskSnapshotRecord,
    SymbolRecord,
)
from services.shadow_engine import ShadowDecisionEngine


@dataclass(frozen=True, slots=True)
class ReplayReport:
    candles_processed: int
    decisions_generated: int
    buy_count: int
    sell_count: int
    no_trade_count: int
    errors: int
    duplicates: int


@dataclass(frozen=True, slots=True)
class SnapshotCursor:
    timestamp: datetime
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class PersistedSnapshotPage:
    rows: tuple[tuple[MarketSnapshot, UUID, RiskSnapshot | None], ...]
    next_cursor: SnapshotCursor | None
    has_more: bool
    rows_examined: int


def load_persisted_snapshots(database, *, limit: int | None = 1_000):
    """Reconstruct replay inputs solely from persisted closed candle records."""

    with database.session() as session:
        query = _snapshot_rows_query(stable_order=False)
        if limit is not None:
            query = query.limit(limit)
        rows = session.execute(query).all()
        return _reconstruct_snapshot_rows(session, rows)


def load_persisted_snapshot_page(
    database,
    *,
    limit: int,
    after: SnapshotCursor | None = None,
) -> PersistedSnapshotPage:
    """Reconstruct one bounded, stable keyset page of persisted snapshots."""

    if limit < 1:
        raise ValueError("snapshot page limit must be positive")
    with database.session() as session:
        query = _snapshot_rows_query(stable_order=True)
        if after is not None:
            query = query.where(
                or_(
                    MarketSnapshotRecord.timestamp > after.timestamp,
                    and_(
                        MarketSnapshotRecord.timestamp == after.timestamp,
                        MarketSnapshotRecord.id > after.snapshot_id,
                    ),
                )
            )
        # The extra raw row establishes has_more without reconstructing it.
        rows = session.execute(query.limit(limit + 1)).all()
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_cursor = None
        if selected:
            last_market = selected[-1][0]
            next_cursor = SnapshotCursor(last_market.timestamp, last_market.id)
        reconstructed = _reconstruct_snapshot_rows(session, selected)
        return PersistedSnapshotPage(
            rows=tuple(reconstructed),
            next_cursor=next_cursor,
            has_more=has_more,
            rows_examined=len(selected),
        )


def _snapshot_rows_query(*, stable_order: bool):
    query = (
        select(MarketSnapshotRecord, SymbolRecord, AccountSnapshotRecord, AccountRecord)
        .join(SymbolRecord, SymbolRecord.id == MarketSnapshotRecord.symbol_id)
        .join(
            AccountSnapshotRecord,
            AccountSnapshotRecord.id == MarketSnapshotRecord.account_snapshot_id,
        )
        .join(AccountRecord, AccountRecord.id == AccountSnapshotRecord.account_id)
    )
    if stable_order:
        return query.order_by(MarketSnapshotRecord.timestamp, MarketSnapshotRecord.id)
    return query.order_by(MarketSnapshotRecord.timestamp)


def _reconstruct_snapshot_rows(session, rows):
    result = []
    for market, symbol_row, account_row, account_record in rows:
        candle_map: dict[Timeframe, tuple[Candle, ...]] = {}
        for timeframe in Timeframe:
            candle_rows = list(
                session.scalars(
                    select(CandleRecord)
                    .where(
                        CandleRecord.symbol_id == symbol_row.id,
                        CandleRecord.timeframe == timeframe.value,
                        CandleRecord.timestamp <= market.timestamp,
                    )
                    .order_by(CandleRecord.timestamp.desc())
                    .limit(100)
                )
            )[::-1]
            if len(candle_rows) < 2:
                break
            candle_map[timeframe] = tuple(
                Candle(
                    timestamp=item.timestamp,
                    raw_timestamp=item.raw_timestamp,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    tick_volume=item.tick_volume,
                    spread=item.spread,
                    real_volume=item.real_volume,
                )
                for item in candle_rows
            )
        if len(candle_map) != len(Timeframe):
            continue
        account = AccountState(
            balance=account_row.balance,
            equity=account_row.equity,
            margin=account_row.margin,
            free_margin=account_row.free_margin,
            margin_level=account_row.margin_level,
            profit=account_row.profit,
            leverage=account_row.leverage,
            currency=account_record.currency,
            server=account_record.server,
            trade_mode=account_record.trade_mode,
        )
        symbol = SymbolSpecification(
            name=symbol_row.name,
            bid=market.bid,
            ask=market.ask,
            spread=market.spread,
            digits=symbol_row.digits,
            point=symbol_row.point,
            trade_tick_size=symbol_row.trade_tick_size,
            trade_tick_value=symbol_row.trade_tick_value,
            trade_tick_value_profit=symbol_row.trade_tick_value_profit,
            trade_tick_value_loss=symbol_row.trade_tick_value_loss,
            contract_size=symbol_row.contract_size,
            volume_min=symbol_row.volume_min,
            volume_max=symbol_row.volume_max,
            volume_step=symbol_row.volume_step,
            trade_mode=symbol_row.trade_mode,
            trade_mode_name=symbol_row.trade_mode_name,
        )
        positions = []
        position_rows = session.execute(
            select(PositionRecord, PositionSnapshotRecord)
            .join(
                PositionSnapshotRecord,
                PositionSnapshotRecord.position_id == PositionRecord.id,
            )
            .where(PositionSnapshotRecord.market_snapshot_id == market.id)
        ).all()
        for position, observed in position_rows:
            positions.append(
                Position(
                    ticket=position.broker_ticket,
                    symbol=symbol.name,
                    type=0 if position.direction == "BUY" else 1,
                    type_name=position.direction,
                    volume=observed.volume,
                    open_price=observed.open_price,
                    current_price=observed.current_price,
                    stop_loss=observed.stop_loss,
                    take_profit=observed.take_profit,
                    profit=observed.profit,
                    swap=observed.swap,
                    magic_number=position.magic_number,
                    comment=position.comment,
                    open_time=position.broker_open_time,
                    raw_open_time=int(position.broker_open_time.timestamp()),
                )
            )
        snapshot = MarketSnapshot(
            account=account,
            symbol=symbol,
            tick=Tick(
                timestamp=market.timestamp,
                raw_timestamp=int(market.timestamp.timestamp()),
                bid=market.bid,
                ask=market.ask,
                last=market.bid,
                volume=0,
                flags=0,
            ),
            positions=tuple(positions),
            candles=candle_map,
            generated_at=market.timestamp,
        )
        risk_row = session.scalar(
            select(RiskSnapshotRecord)
            .where(RiskSnapshotRecord.market_snapshot_id == market.id)
            .limit(1)
        )
        risk = None
        if risk_row is not None:
            risk = RiskSnapshot(
                timestamp=risk_row.timestamp,
                equity=risk_row.equity,
                balance=risk_row.balance,
                open_risk_percent=risk_row.open_risk_percent,
                open_risk_amount=risk_row.open_risk_amount,
                remaining_risk_percent=risk_row.remaining_risk_percent,
                risk_per_position=tuple(
                    PositionRisk(**item) for item in risk_row.risk_per_position
                ),
                daily_pnl=risk_row.daily_pnl,
                daily_realized_loss=risk_row.daily_realized_loss,
                drawdown_percent=risk_row.drawdown_percent,
                max_trade_risk_percent=risk_row.max_trade_risk_percent,
                max_aggregate_risk_percent=risk_row.max_aggregate_risk_percent,
                open_positions_count=risk_row.open_positions_count,
                unbounded_positions_count=risk_row.unbounded_positions_count,
                margin_usage_percent=risk_row.margin_usage_percent,
                free_margin=risk_row.free_margin,
            )
        result.append((snapshot, UUID(market.id), risk))
    return result


def replay_snapshots(
    snapshots: Iterable[tuple[MarketSnapshot, UUID | None, RiskSnapshot | None]],
    *,
    engine: ShadowDecisionEngine | None = None,
) -> ReplayReport:
    """Evaluate snapshots in chronological order, exactly once per M5 candle."""

    decision_engine = engine or ShadowDecisionEngine()

    def m5_timestamp(snapshot: MarketSnapshot):
        return ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=True)

    ordered = sorted(snapshots, key=lambda item: m5_timestamp(item[0]))
    seen: set[tuple[str, object, str]] = set()
    counts = {ShadowAction.BUY: 0, ShadowAction.SELL: 0, ShadowAction.NO_TRADE: 0}
    errors = duplicates = generated = processed = 0
    for snapshot, snapshot_id, risk in ordered:
        candles = snapshot.candles.get(Timeframe.M5, ())
        if not candles:
            errors += 1
            continue
        candle_key = (
            snapshot.symbol.name,
            m5_timestamp(snapshot),
            decision_engine.strategy_version,
        )
        processed += 1
        if candle_key in seen:
            duplicates += 1
            continue
        seen.add(candle_key)
        try:
            decision = decision_engine.evaluate(
                snapshot,
                market_snapshot_id=snapshot_id,
                risk=risk,
                candles_are_closed=True,
            )
        except Exception:
            errors += 1
            continue
        counts[decision.decision] += 1
        generated += 1
    return ReplayReport(
        candles_processed=processed,
        decisions_generated=generated,
        buy_count=counts[ShadowAction.BUY],
        sell_count=counts[ShadowAction.SELL],
        no_trade_count=counts[ShadowAction.NO_TRADE],
        errors=errors,
        duplicates=duplicates,
    )
