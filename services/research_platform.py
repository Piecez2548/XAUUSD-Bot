"""Persistent, deterministic historical research platform.

This module is deliberately isolated from the live observatory.  It copies
closed operational candles into immutable research datasets, records every
decision and outcome, and never imports or calls a broker write API.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import desc, select

from models.market import (
    AccountState,
    Candle,
    MarketSnapshot,
    SymbolSpecification,
    Tick,
    Timeframe,
)
from models.observatory import RiskSnapshot
from mt5.connection import MT5Connection
from mt5.market_data import read_completed_candles_paginated
from mt5.symbols import discover_symbol
from persistence.orm import (
    AccountRecord,
    AccountSnapshotRecord,
    CandleRecord,
    ResearchCandleRecord,
    ResearchDatasetRecord,
    ResearchDecisionRecord,
    ResearchMetricRecord,
    ResearchOutcomeRecord,
    ResearchRunRecord,
    ShadowDecisionRecord,
    SymbolRecord,
)
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_outcome import EvaluationPolicy, evaluate_decision
from services.shadow_replay import load_persisted_snapshots
from services.strategy_platform import StrategyRegistry

ENGINE_VERSION = "research_engine_v1"
RR_GRID = (1.0, 1.5, 2.0, 2.5, 3.0)


@dataclass(frozen=True, slots=True)
class DatasetImportResult:
    dataset_id: str
    dataset_hash: str
    symbol: str
    timeframe: str
    row_count: int
    start_at: datetime
    end_at: datetime
    duplicate_rows: int
    invalid_rows: int
    conflicting_rows: int
    gap_count: int = 0
    missing_intervals: int = 0
    approximate_trading_days: float = 0.0


def _git_trace(project_root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_root, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "diff", "--quiet"], cwd=project_root,
            capture_output=True, check=False,
        ).returncode != 0
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _canonical_candle(row: CandleRecord) -> str:
    return json.dumps(
        {
            "timestamp": row.timestamp.astimezone(UTC).isoformat(),
            "open": row.open, "high": row.high, "low": row.low, "close": row.close,
            "tick_volume": row.tick_volume, "spread": row.spread, "real_volume": row.real_volume,
        }, sort_keys=True, separators=(",", ":"),
    )


def _gap_stats(rows: list[object], timeframe: str) -> tuple[int, int, float]:
    expected = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}[timeframe]
    stamps = sorted(int(item.timestamp.timestamp()) for item in rows)
    gaps = [
        max(0, (right - left) // expected - 1)
        for left, right in zip(stamps, stamps[1:], strict=False)
    ]
    elapsed_days = (stamps[-1] - stamps[0]) / 86_400 if stamps else 0.0
    # This is calendar coverage, not a claim that every day had a session.
    return sum(value > 0 for value in gaps), sum(gaps), elapsed_days / 7 * 5 if elapsed_days else 0.0


def _persist_candle_objects(
    database,
    candles: tuple[Candle, ...],
    *,
    symbol: str,
    timeframe: str,
    source: str,
    provenance: dict[str, object],
) -> DatasetImportResult:
    ordered = sorted(candles, key=lambda item: (item.timestamp.astimezone(UTC), item.raw_timestamp))
    by_timestamp: dict[datetime, Candle] = {}
    duplicate_rows = conflicting_rows = 0
    for candle in ordered:
        timestamp = candle.timestamp.astimezone(UTC)
        if timestamp in by_timestamp:
            duplicate_rows += 1
            previous = by_timestamp[timestamp]
            if _canonical_candle(previous) != _canonical_candle(candle):
                conflicting_rows += 1
            continue
        by_timestamp[timestamp] = candle
    rows = [by_timestamp[key] for key in sorted(by_timestamp)]
    payload = "\n".join(_canonical_candle(row) for row in rows).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    gap_count, missing_intervals, trading_days = _gap_stats(rows, timeframe)
    with database.session() as session:
        existing = session.scalar(select(ResearchDatasetRecord).where(ResearchDatasetRecord.dataset_hash == digest))
        if existing is not None:
            metadata = existing.metadata_json or {}
            return DatasetImportResult(
                existing.dataset_id, existing.dataset_hash, existing.symbol, existing.timeframe,
                existing.row_count, existing.start_at, existing.end_at, duplicate_rows, 0,
                conflicting_rows, int(metadata.get("gap_count", gap_count)),
                int(metadata.get("missing_intervals", missing_intervals)),
                float(metadata.get("approximate_trading_days", trading_days)),
            )
        dataset = ResearchDatasetRecord(
            dataset_id=f"dataset_{digest[:12]}", symbol=symbol, timeframe=timeframe,
            source=source, start_at=rows[0].timestamp, end_at=rows[-1].timestamp,
            row_count=len(rows), dataset_hash=digest, timezone="UTC", closed_candles_only=True,
            metadata_json={**provenance, "dedupe": "timestamp_first_deterministic",
                           "duplicate_rows": duplicate_rows, "conflicting_rows": conflicting_rows,
                           "invalid_rows": 0, "gap_count": gap_count,
                           "missing_intervals": missing_intervals,
                           "approximate_trading_days": trading_days, "gaps_not_filled": True},
        )
        session.add(dataset)
        session.flush()
        for candle in rows:
            session.add(ResearchCandleRecord(
                dataset_id=dataset.id, symbol=symbol, timeframe=timeframe,
                timestamp=candle.timestamp, raw_timestamp=candle.raw_timestamp,
                open=candle.open, high=candle.high, low=candle.low, close=candle.close,
                tick_volume=candle.tick_volume, spread=candle.spread, real_volume=candle.real_volume,
                provenance_json=provenance,
            ))
        return DatasetImportResult(
            dataset.dataset_id, digest, symbol, timeframe, len(rows), rows[0].timestamp,
            rows[-1].timestamp, duplicate_rows, 0, conflicting_rows, gap_count,
            missing_intervals, trading_days,
        )


def load_research_snapshots(database, settings, *, dataset_id: str,
                            max_m5_rows: int | None = None):
    """Build causal, candle-only replay snapshots from imported datasets.

    Account/symbol context is explicitly the latest observed read-only context;
    no historical broker state is invented and no positions are opened.
    """
    with database.session() as session:
        selected = session.scalar(
            select(ResearchDatasetRecord).where(ResearchDatasetRecord.dataset_id == dataset_id)
        )
        if selected is None:
            raise ValueError(f"research dataset not found: {dataset_id}")
        datasets = session.scalars(
            select(ResearchDatasetRecord)
            .where(
                ResearchDatasetRecord.symbol == selected.symbol,
                ResearchDatasetRecord.source == selected.source,
            )
            .order_by(desc(ResearchDatasetRecord.created_at))
        ).all()
        dataset_by_tf = {row.timeframe: row for row in datasets}
        missing = [tf.value for tf in Timeframe if tf.value not in dataset_by_tf]
        if missing:
            raise ValueError(
                "research replay requires imported datasets for " + ", ".join(missing)
            )
        raw_rows_by_tf: dict[str, list[ResearchCandleRecord]] = {}
        rows_by_tf: dict[str, list[Candle]] = {}
        for timeframe in Timeframe:
            source = dataset_by_tf[timeframe.value]
            rows = session.scalars(
                select(ResearchCandleRecord)
                .where(ResearchCandleRecord.dataset_id == source.id)
                .order_by(ResearchCandleRecord.timestamp)
            ).all()
            raw_rows_by_tf[timeframe.value] = rows
            rows_by_tf[timeframe.value] = [
                Candle(timestamp=row.timestamp, raw_timestamp=row.raw_timestamp, open=row.open,
                       high=row.high, low=row.low, close=row.close, tick_volume=row.tick_volume,
                       spread=row.spread, real_volume=row.real_volume)
                for row in rows
            ]
        symbol_row = session.scalar(select(SymbolRecord).where(SymbolRecord.name == selected.symbol))
        account_row = session.scalar(select(AccountSnapshotRecord).order_by(desc(AccountSnapshotRecord.timestamp)))
        account_record = session.scalar(select(AccountRecord).where(AccountRecord.id == account_row.account_id)) if account_row else None
        if symbol_row is None or account_row is None or account_record is None:
            raise ValueError("research replay requires an observed symbol and account context")
        m5 = rows_by_tf["M5"]
        if max_m5_rows is not None:
            if max_m5_rows <= 0:
                raise ValueError("max_m5_rows must be greater than zero")
            # Benchmark/recovery callers may bound the replay window.  The
            # underlying imported datasets remain immutable and the default
            # path still replays every available closed M5 candle.
            m5 = m5[:max_m5_rows]
        stamps_by_tf = {key: [item.timestamp for item in series] for key, series in rows_by_tf.items()}
        snapshots = []
        risk = RiskSnapshot(
            equity=account_row.equity, balance=account_row.balance, free_margin=account_row.free_margin,
            risk_per_position=tuple(), max_trade_risk_percent=min(settings.max_trade_risk_percent, 2.0),
            max_aggregate_risk_percent=min(settings.max_aggregate_risk_percent, 6.0),
            open_positions_count=0, unbounded_positions_count=0, open_risk_percent=0.0,
            open_risk_amount=0.0, remaining_risk_percent=min(settings.max_aggregate_risk_percent, 6.0),
        )
        account = AccountState(
            balance=account_row.balance, equity=account_row.equity, margin=account_row.margin,
            free_margin=account_row.free_margin, margin_level=account_row.margin_level,
            profit=account_row.profit, leverage=account_row.leverage, currency=account_record.currency,
            server=account_record.server, trade_mode=account_record.trade_mode, trade_mode_name="research",
        )
        for candle in m5:
            timestamp = candle.timestamp
            candles: dict[Timeframe, tuple[Candle, ...]] = {}
            for timeframe in Timeframe:
                series = rows_by_tf[timeframe.value]
                index = bisect_right(stamps_by_tf[timeframe.value], timestamp)
                # Keep the same bounded feature context as the live engine.
                candles[timeframe] = tuple(series[max(0, index - 100):index])
            if any(not candles[timeframe] for timeframe in Timeframe):
                continue
            symbol = SymbolSpecification(
                name=selected.symbol, bid=candle.close, ask=candle.close,
                spread=20, digits=symbol_row.digits,
                point=symbol_row.point, trade_tick_size=symbol_row.trade_tick_size,
                trade_tick_value=symbol_row.trade_tick_value,
                trade_tick_value_profit=symbol_row.trade_tick_value_profit,
                trade_tick_value_loss=symbol_row.trade_tick_value_loss,
                contract_size=symbol_row.contract_size, volume_min=symbol_row.volume_min,
                volume_max=symbol_row.volume_max, volume_step=symbol_row.volume_step,
                trade_mode=symbol_row.trade_mode, trade_mode_name=symbol_row.trade_mode_name,
            )
            snapshot = MarketSnapshot(
                account=account, symbol=symbol,
                tick=Tick(timestamp=timestamp, raw_timestamp=candle.raw_timestamp,
                          bid=candle.close, ask=candle.close, last=candle.close, volume=0, flags=0),
                positions=tuple(), candles=candles, generated_at=timestamp,
            )
            snapshot_id = uuid5(NAMESPACE_URL, f"research:{selected.dataset_hash}:{timestamp.isoformat()}")
            snapshots.append((snapshot, snapshot_id, risk))
        return snapshots, raw_rows_by_tf["M5"]


def import_research_dataset(database, *, timeframe: str = "M5", symbol: str | None = None) -> DatasetImportResult:
    """Copy normalized closed candles into a new immutable dataset manifest."""
    timeframe = timeframe.upper()
    with database.session() as session:
        query = (
            select(CandleRecord, SymbolRecord)
            .join(SymbolRecord, CandleRecord.symbol_id == SymbolRecord.id)
            .where(CandleRecord.timeframe == timeframe)
            .order_by(CandleRecord.timestamp, CandleRecord.id)
        )
        if symbol:
            query = query.where(SymbolRecord.name == symbol)
        rows = session.execute(query).all()
        if not rows:
            raise ValueError(f"no closed {timeframe} candles available for research import")
        selected_symbol = symbol or rows[0][1].name
        canonical: dict[datetime, CandleRecord] = {}
        duplicate_rows = conflicting_rows = invalid_rows = 0
        for row, symbol_row in rows:
            if symbol_row.name != selected_symbol:
                continue
            timestamp = row.timestamp.astimezone(UTC)
            if not (row.high >= max(row.open, row.close, row.low) and row.low <= min(row.open, row.close, row.high)):
                invalid_rows += 1
                continue
            if timestamp in canonical:
                duplicate_rows += 1
                if _canonical_candle(canonical[timestamp]) != _canonical_candle(row):
                    conflicting_rows += 1
                continue
            canonical[timestamp] = row
        ordered = [canonical[key] for key in sorted(canonical)]
        if not ordered:
            raise ValueError("research import produced no valid closed candles")
        payload = "\n".join(_canonical_candle(row) for row in ordered).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        existing = session.scalar(select(ResearchDatasetRecord).where(ResearchDatasetRecord.dataset_hash == digest))
        if existing is not None:
            return DatasetImportResult(existing.dataset_id, existing.dataset_hash, existing.symbol, existing.timeframe,
                                       existing.row_count, existing.start_at, existing.end_at, duplicate_rows, invalid_rows, conflicting_rows)
        dataset = ResearchDatasetRecord(
            dataset_id=f"dataset_{digest[:12]}", symbol=selected_symbol, timeframe=timeframe,
            source="operational_candles", start_at=ordered[0].timestamp, end_at=ordered[-1].timestamp,
            row_count=len(ordered), dataset_hash=digest, timezone="UTC", closed_candles_only=True,
            metadata_json={"source_table": "candles", "dedupe": "timestamp_first_deterministic",
                           "duplicate_rows": duplicate_rows, "conflicting_rows": conflicting_rows,
                           "invalid_rows": invalid_rows, "gaps_not_filled": True},
        )
        session.add(dataset)
        session.flush()
        for row in ordered:
            session.add(ResearchCandleRecord(
                dataset_id=dataset.id, symbol=selected_symbol, timeframe=timeframe,
                timestamp=row.timestamp, raw_timestamp=row.raw_timestamp, open=row.open,
                high=row.high, low=row.low, close=row.close, tick_volume=row.tick_volume,
                spread=row.spread, real_volume=row.real_volume,
                provenance_json={"source_table": "candles", "source_row_id": row.id},
            ))
        return DatasetImportResult(dataset.dataset_id, digest, selected_symbol, timeframe,
                                   len(ordered), ordered[0].timestamp, ordered[-1].timestamp,
                                   duplicate_rows, invalid_rows, conflicting_rows)


def import_mt5_research_dataset(
    database,
    settings,
    *,
    timeframe: str = "M5",
    count: int = 50_000,
    chunk_size: int = 5_000,
) -> DatasetImportResult:
    """Import the largest bounded closed-candle range MT5 can provide."""
    timeframe = timeframe.upper()
    if timeframe not in {item.value for item in Timeframe}:
        raise ValueError(f"unsupported research timeframe: {timeframe}")
    with MT5Connection(settings) as connection:
        symbol, _ = discover_symbol(connection.api, settings.trading_symbol)
        candles = read_completed_candles_paginated(
            connection.api, symbol, Timeframe(timeframe), count, chunk_size=chunk_size
        )
    if not candles:
        raise ValueError(f"MT5 returned no closed {symbol} {timeframe} candles")
    return _persist_candle_objects(
        database, candles, symbol=symbol, timeframe=timeframe,
        source="mt5_copy_rates_from_pos", provenance={
            "source_api": "copy_rates_from_pos", "closed_position_start": 1,
            "requested_count": count, "chunk_size": chunk_size,
            "terminal_symbol": symbol, "execution_allowed": False,
        },
    )


def import_mt5_research_bundle(
    database,
    settings,
    *,
    count: int = 50_000,
    chunk_size: int = 5_000,
) -> list[DatasetImportResult]:
    """Import M5 plus all strategy-required higher-timeframe datasets."""
    return [
        import_mt5_research_dataset(
            database, settings, timeframe=timeframe, count=count, chunk_size=chunk_size
        )
        for timeframe in ("M5", "M15", "H1", "H4")
    ]


def build_split_definition(start_at: datetime, end_at: datetime) -> dict[str, object]:
    total = (end_at - start_at).total_seconds()
    if total <= 0:
        return {"policy": "chronological_60_20_20", "status": "INSUFFICIENT_DATA", "windows": []}
    points = [start_at + timedelta(seconds=total * ratio) for ratio in (0.6, 0.8)]
    status = (
        "READY_FOR_HOLDOUT_VALIDATION"
        if total >= 180 * 86_400
        else "INSUFFICIENT_DATA_FOR_HOLDOUT_VALIDATION"
    )
    return {
        "policy": "chronological_60_20_20",
        "status": status,
        "windows": [
            {"name": "development", "start": start_at.isoformat(), "end": points[0].isoformat()},
            {"name": "validation", "start": points[0].isoformat(), "end": points[1].isoformat()},
            {"name": "holdout", "start": points[1].isoformat(), "end": end_at.isoformat()},
        ],
    }


def build_walk_forward_windows(start_at: datetime, end_at: datetime, *, train_days: int = 30, test_days: int = 7) -> list[dict[str, str]]:
    windows: list[dict[str, str]] = []
    cursor = start_at
    while cursor + timedelta(days=train_days + test_days) <= end_at:
        train_end = cursor + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)
        windows.append({"train_start": cursor.isoformat(), "train_end": train_end.isoformat(),
                        "test_start": train_end.isoformat(), "test_end": test_end.isoformat()})
        cursor += timedelta(days=test_days)
    return windows


def _set_rr(strategy, rr: float) -> None:
    engine = getattr(strategy, "engine", None)
    if engine is not None and hasattr(engine, "target_rr"):
        engine.target_rr = rr
    if hasattr(strategy, "target_rr"):
        strategy.target_rr = rr


def _temporary_shadow_decision(decision) -> ShadowDecisionRecord:
    return ShadowDecisionRecord(
        id=str(decision.decision_id), created_at=decision.created_at,
        market_snapshot_id=str(decision.market_snapshot_id) if decision.market_snapshot_id else None,
        symbol=decision.symbol, m5_candle_timestamp=decision.m5_candle_timestamp,
        decision=decision.decision.value, market_regime=decision.market_regime.value,
        entry_price=decision.entry_price, stop_loss=decision.stop_loss, take_profit=decision.take_profit,
        risk_reward_ratio=decision.risk_reward_ratio, requested_risk_percent=decision.requested_risk_percent,
        approved_risk_percent=decision.approved_risk_percent, hypothetical_volume=decision.hypothetical_volume,
        confidence=decision.confidence, strategy_name=decision.strategy_name,
        strategy_version=decision.strategy_version, config_version=decision.config_version,
        config_hash=decision.config_hash, reason_codes=list(decision.reason_codes),
        human_readable_reason=decision.human_readable_reason, risk_gate_state=decision.risk_gate_state,
        data_freshness=decision.data_freshness, execution_allowed=False,
        feature_context=decision.feature_context, outcome_status="PENDING",
    )


def _run_backtest_legacy(database, settings, *, project_root: Path, strategy_id: str = "trend_pullback_v1",
                 dataset_id: str | None = None, rr: float | None = None,
                 split: str = "all", run_name: str | None = None,
                 existing_run_id: str | None = None) -> list[dict[str, object]]:
    imported = None if dataset_id else import_research_dataset(
        database, timeframe="M5", symbol=settings.trading_symbol
    )
    with database.session() as session:
        selected_dataset_id = dataset_id or (imported.dataset_id if imported else None)
        dataset = session.scalar(
            select(ResearchDatasetRecord).where(ResearchDatasetRecord.dataset_id == selected_dataset_id)
        )
        if dataset is None:
            raise ValueError("research dataset not found")
        dataset_id_internal = dataset.id
        dataset_hash = dataset.dataset_hash
        symbol = dataset.symbol
        split_definition = build_split_definition(dataset.start_at, dataset.end_at)
    strategy = StrategyRegistry(settings).resolve(strategy_id)
    values = (rr,) if rr is not None else RR_GRID
    if dataset.source == "mt5_copy_rates_from_pos":
        snapshots, research_m5_candles = load_research_snapshots(
            database, settings, dataset_id=dataset.dataset_id
        )
    else:
        snapshots = load_persisted_snapshots(database, limit=None)
        research_m5_candles = None
    ordered = sorted(snapshots, key=lambda item: ShadowDecisionEngine._m5_timestamp(item[0], candles_are_closed=True))
    git_commit, git_dirty = _git_trace(project_root)
    outputs: list[dict[str, object]] = []
    for rr_value in values:
        if rr_value <= 0:
            raise ValueError("rr must be greater than zero")
        _set_rr(strategy, float(rr_value))
        run_uuid = existing_run_id if existing_run_id and len(values) == 1 else str(uuid4())
        started = datetime.now(UTC)
        with database.session() as session:
            run = session.scalar(select(ResearchRunRecord).where(ResearchRunRecord.run_id == run_uuid)) if existing_run_id and len(values) == 1 else None
            if run is not None:
                run.status = "RUNNING"
                run.started_at = started
                run.parameters_json = {"rr": float(rr_value), "split": split, "rr_grid": list(RR_GRID)}
            else:
                run = ResearchRunRecord(
                run_id=run_uuid, run_name=run_name, strategy_id=strategy.metadata.strategy_id,
                strategy_version=strategy.metadata.strategy_version, config_hash=strategy.metadata.config_hash,
                dataset_id=dataset_id_internal, dataset_hash=dataset_hash, symbol=symbol, timeframe="M5",
                status="RUNNING", started_at=started, git_commit=git_commit, git_dirty=git_dirty,
                engine_version=ENGINE_VERSION, parameters_json={"rr": float(rr_value), "split": split,
                                                                  "rr_grid": list(RR_GRID)},
                split_definition=split_definition, summary_json={}, execution_allowed=False,
                )
                session.add(run)
            session.flush()
            counts: Counter[str] = Counter()
            reasons: Counter[str] = Counter()
            decisions: list[tuple[ResearchDecisionRecord, object]] = []
            seen: set[tuple[str, datetime]] = set()
            for snapshot, snapshot_id, risk in ordered:
                timestamp = ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=True)
                if snapshot.symbol.name != symbol or timestamp < dataset.start_at or timestamp > dataset.end_at:
                    continue
                if split != "all":
                    split_window = next(
                        (item for item in split_definition.get("windows", []) if item.get("name") == split),
                        None,
                    )
                    if split_window is None:
                        raise ValueError(f"unknown split: {split}")
                    split_start = datetime.fromisoformat(str(split_window["start"]))
                    split_end = datetime.fromisoformat(str(split_window["end"]))
                    if timestamp < split_start or timestamp > split_end:
                        continue
                if (snapshot.symbol.name, timestamp) in seen:
                    continue
                seen.add((snapshot.symbol.name, timestamp))
                decision = strategy.evaluate(snapshot, market_snapshot_id=snapshot_id, risk=risk, candles_are_closed=True)
                reason = (decision.reason_codes or ("NONE",))[0]
                counts[decision.decision.value] += 1
                reasons[reason] += 1
                record = ResearchDecisionRecord(
                    run_id=run.id, timestamp=timestamp, decision=decision.decision.value,
                    reason_code=reason, market_regime=decision.market_regime.value,
                    entry_price=decision.entry_price, stop_loss=decision.stop_loss, take_profit=decision.take_profit,
                    risk_reward_ratio=decision.risk_reward_ratio, confidence=decision.confidence,
                    feature_context=decision.feature_context, rejection_stage=decision.feature_context.get("rejection_stage") if isinstance(decision.feature_context, dict) else None,
                    execution_allowed=False,
                )
                session.add(record)
                decisions.append((record, decision))
            session.flush()
            if research_m5_candles is not None:
                m5_candles = research_m5_candles
            else:
                m5_candles = session.scalars(
                    select(CandleRecord)
                    .join(SymbolRecord, CandleRecord.symbol_id == SymbolRecord.id)
                    .where(SymbolRecord.name == symbol, CandleRecord.timeframe == "M5")
                    .order_by(CandleRecord.timestamp, CandleRecord.id)
                ).all()
            m5_timestamps = [candle.timestamp for candle in m5_candles]
            for record, decision in decisions:
                if decision.decision.value not in {"BUY", "SELL"}:
                    continue
                start_index = bisect_right(m5_timestamps, decision.m5_candle_timestamp)
                future_candles = m5_candles[
                    start_index : start_index + settings.shadow_outcome_horizon_bars
                ]
                values = evaluate_decision(
                    _temporary_shadow_decision(decision), future_candles,
                    policy=EvaluationPolicy(horizon_bars=settings.shadow_outcome_horizon_bars),
                )
                session.add(ResearchOutcomeRecord(
                    run_id=run.id, decision_id=record.id, policy_version="research_shadow_outcome_v1",
                    terminal_status=str(values["terminal_status"]), realized_r=values["realized_r"],
                    bars_held=int(values["bars_held"]), mfe_r=values["mfe_r"], mae_r=values["mae_r"],
                    terminal_candle_timestamp=values["terminal_candle_timestamp"],
                    reason_code=str(values["reason_code"]), evaluated_at=values["evaluated_at"],
                ))
            summary = {"eligible_candles": len(seen), "buy": counts["BUY"], "sell": counts["SELL"],
                       "no_trade": counts["NO_TRADE"], "reason_counts": dict(reasons),
                       "execution_allowed": False, "orders_sent": 0, "broker_writes": 0,
                       "funnel": {"snapshots": len(seen), "decisions": len(decisions), "eligible": counts["BUY"] + counts["SELL"]}}
            run.status = "COMPLETED"
            run.completed_at = datetime.now(UTC)
            run.summary_json = summary
            for key, value in (("eligible_candles", len(seen)), ("buy", counts["BUY"]),
                               ("sell", counts["SELL"]), ("no_trade", counts["NO_TRADE"])):
                session.add(ResearchMetricRecord(run_id=run.id, metric_key=key, value=float(value), denominator=len(seen), dimension_json={}))
            outputs.append({"run_id": run_uuid, "strategy_id": strategy.metadata.strategy_id,
                            "strategy_version": strategy.metadata.strategy_version, "rr": float(rr_value),
                            "dataset_id": dataset.dataset_id, "dataset_hash": dataset_hash, **summary,
                            "status": run.status, "split_definition": split_definition})
    return outputs


def _research_inputs(database, settings, dataset_id: str | None,
                     max_m5_rows: int | None = None):
    imported = None if dataset_id else import_research_dataset(
        database, timeframe="M5", symbol=settings.trading_symbol
    )
    selected_dataset_id = dataset_id or (imported.dataset_id if imported else None)
    with database.session() as session:
        dataset = session.scalar(
            select(ResearchDatasetRecord).where(ResearchDatasetRecord.dataset_id == selected_dataset_id)
        )
        if dataset is None:
            raise ValueError("research dataset not found")
        dataset_data = {
            "id": dataset.id, "dataset_id": dataset.dataset_id, "dataset_hash": dataset.dataset_hash,
            "source": dataset.source, "symbol": dataset.symbol, "start_at": dataset.start_at,
            "end_at": dataset.end_at, "split_definition": build_split_definition(dataset.start_at, dataset.end_at),
        }
    if dataset_data["source"] == "mt5_copy_rates_from_pos":
        snapshots, research_m5_candles = load_research_snapshots(
            database, settings, dataset_id=dataset_data["dataset_id"],
            max_m5_rows=max_m5_rows,
        )
    else:
        snapshots = load_persisted_snapshots(database, limit=None)
        research_m5_candles = None
    ordered = sorted(
        snapshots,
        key=lambda item: ShadowDecisionEngine._m5_timestamp(item[0], candles_are_closed=True),
    )
    return dataset_data, ordered, research_m5_candles


def _rr_decision(decision, rr: float):
    if decision.decision.value not in {"BUY", "SELL"}:
        return decision
    assert decision.entry_price is not None and decision.stop_loss is not None
    distance = (
        decision.entry_price - decision.stop_loss
        if decision.decision.value == "BUY"
        else decision.stop_loss - decision.entry_price
    )
    target = (
        decision.entry_price + distance * rr
        if decision.decision.value == "BUY"
        else decision.entry_price - distance * rr
    )
    # A signal intent is reusable, but each persisted RR run owns its
    # decision rows.  Give the copied decision a fresh identifier so the
    # immutable primary key cannot collide across RR values.
    return decision.model_copy(update={
        "decision_id": uuid4(), "take_profit": target, "risk_reward_ratio": rr
    })


def _outcome_metrics(outcomes: list[dict[str, object]], *, signal_count: int) -> dict[str, object]:
    terminal = [item for item in outcomes if item["terminal_status"] in {"TP_HIT", "SL_HIT", "AMBIGUOUS", "EXPIRED"}]
    realized = [float(item["realized_r"]) for item in terminal if item["realized_r"] is not None]
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    equity = peak = max_drawdown = 0.0
    current_loss = current_win = max_loss = max_win = 0
    for value in realized:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if value < 0:
            current_loss += 1
            current_win = 0
        elif value > 0:
            current_win += 1
            current_loss = 0
        else:
            current_loss = current_win = 0
        max_loss = max(max_loss, current_loss)
        max_win = max(max_win, current_win)
    total = sum(realized)
    gross_loss = abs(sum(losses))
    counts = Counter(str(item["terminal_status"]) for item in outcomes)
    return {
        "trades": signal_count,
        "tp": counts["TP_HIT"], "sl": counts["SL_HIT"],
        "ambiguous": counts["AMBIGUOUS"], "expired": counts["EXPIRED"],
        "pending": counts["PENDING"],
        "win_rate": len(wins) / len(terminal) if terminal else None,
        "average_r": sum(realized) / len(realized) if realized else None,
        "median_r": sorted(realized)[len(realized) // 2] if realized else None,
        "total_r": total, "profit_factor": sum(wins) / gross_loss if gross_loss else None,
        "max_drawdown_r": max_drawdown, "maximum_consecutive_losses": max_loss,
        "maximum_consecutive_wins": max_win,
        "expectancy": sum(realized) / len(realized) if realized else None,
        "coverage": len(terminal) / signal_count if signal_count else 0.0,
    }


def run_backtest(
    database, settings, *, project_root: Path, strategy_id: str = "trend_pullback_v1",
    dataset_id: str | None = None, rr: float | None = None, split: str = "all",
    run_name: str | None = None, existing_run_id: str | None = None,
    max_m5_rows: int | None = None,
) -> list[dict[str, object]]:
    """Optimized persistent backtest runner.

    Strategy evaluation is performed once at canonical RR=2.0.  Every RR run
    reuses those immutable signal intents and only recomputes target geometry
    and bounded outcomes.  NO_TRADE decisions are represented by aggregate
    funnel counts rather than duplicated rows.
    """
    total_started = time.perf_counter()
    dataset, ordered, research_m5_candles = _research_inputs(
        database, settings, dataset_id, max_m5_rows=max_m5_rows
    )
    strategy = StrategyRegistry(settings).resolve(strategy_id)
    reset_research = getattr(strategy, "reset_research", None)
    if callable(reset_research):
        reset_research()
    split_definition = dataset["split_definition"]
    split_window = None
    if split != "all":
        split_window = next(
            (item for item in split_definition.get("windows", []) if item.get("name") == split), None
        )
        if split_window is None:
            raise ValueError(f"unknown split: {split}")
    canonical_started = time.perf_counter()
    _set_rr(strategy, 2.0)
    canonical: list[tuple[datetime, object, object]] = []
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    seen: set[tuple[str, datetime]] = set()
    for snapshot, snapshot_id, risk in ordered:
        timestamp = ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=True)
        if snapshot.symbol.name != dataset["symbol"] or timestamp < dataset["start_at"] or timestamp > dataset["end_at"]:
            continue
        if split_window is not None:
            split_start = datetime.fromisoformat(str(split_window["start"]))
            split_end = datetime.fromisoformat(str(split_window["end"]))
            if timestamp < split_start or timestamp > split_end:
                continue
        key = (snapshot.symbol.name, timestamp)
        if key in seen:
            continue
        seen.add(key)
        decision = strategy.evaluate(
            snapshot, market_snapshot_id=snapshot_id, risk=risk, candles_are_closed=True
        )
        action = decision.decision.value
        counts[action] += 1
        reasons[(decision.reason_codes or ("NONE",))[0]] += 1
        if action in {"BUY", "SELL"}:
            canonical.append((timestamp, decision, snapshot_id))
    canonical_seconds = time.perf_counter() - canonical_started
    values = (rr,) if rr is not None else RR_GRID
    if research_m5_candles is not None:
        m5_candles = research_m5_candles
    else:
        with database.session() as session:
            m5_candles = session.scalars(
                select(CandleRecord)
                .join(SymbolRecord, CandleRecord.symbol_id == SymbolRecord.id)
                .where(SymbolRecord.name == dataset["symbol"], CandleRecord.timeframe == "M5")
                .order_by(CandleRecord.timestamp, CandleRecord.id)
            ).all()
    m5_timestamps = [candle.timestamp for candle in m5_candles]
    outputs: list[dict[str, object]] = []
    for rr_value in values:
        if rr_value <= 0:
            raise ValueError("rr must be greater than zero")
        run_started = time.perf_counter()
        run_uuid = existing_run_id if existing_run_id and len(values) == 1 else str(uuid4())
        git_commit, git_dirty = _git_trace(project_root)
        signal_decisions = [_rr_decision(decision, float(rr_value)) for _, decision, _ in canonical]
        with database.session() as session:
            run = session.scalar(select(ResearchRunRecord).where(ResearchRunRecord.run_id == run_uuid)) if existing_run_id and len(values) == 1 else None
            if run is None:
                run = ResearchRunRecord(
                    run_id=run_uuid, run_name=run_name, strategy_id=strategy.metadata.strategy_id,
                    strategy_version=strategy.metadata.strategy_version, config_hash=strategy.metadata.config_hash,
                    dataset_id=dataset["id"], dataset_hash=dataset["dataset_hash"], symbol=dataset["symbol"],
                    timeframe="M5", status="RUNNING", started_at=datetime.now(UTC), git_commit=git_commit,
                    git_dirty=git_dirty, engine_version=ENGINE_VERSION,
                    parameters_json={"rr": float(rr_value), "split": split, "rr_grid": list(RR_GRID),
                                     "canonical_signal_rr": 2.0, "no_trade_persistence": "aggregate_only"},
                    split_definition=split_definition, summary_json={}, execution_allowed=False,
                )
                session.add(run)
                session.flush()
            else:
                run.status = "RUNNING"
                run.started_at = datetime.now(UTC)
            decision_records: list[ResearchDecisionRecord] = []
            for decision in signal_decisions:
                decision_records.append(ResearchDecisionRecord(
                    id=str(decision.decision_id), run_id=run.id,
                    timestamp=decision.m5_candle_timestamp, decision=decision.decision.value,
                    reason_code=(decision.reason_codes or ("VALID_SETUP",))[0],
                    market_regime=decision.market_regime.value, entry_price=decision.entry_price,
                    stop_loss=decision.stop_loss, take_profit=decision.take_profit,
                    risk_reward_ratio=decision.risk_reward_ratio, confidence=decision.confidence,
                    feature_context=decision.feature_context, rejection_stage="SIGNAL",
                    execution_allowed=False,
                ))
            persistence_started = time.perf_counter()
            session.bulk_save_objects(decision_records)
            persistence_seconds = time.perf_counter() - persistence_started
            outcomes: list[dict[str, object]] = []
            outcome_started = time.perf_counter()
            for record, decision in zip(decision_records, signal_decisions, strict=True):
                start_index = bisect_right(m5_timestamps, decision.m5_candle_timestamp)
                future_candles = m5_candles[start_index:start_index + settings.shadow_outcome_horizon_bars]
                outcome = evaluate_decision(
                    _temporary_shadow_decision(decision), future_candles,
                    policy=EvaluationPolicy(horizon_bars=settings.shadow_outcome_horizon_bars),
                )
                outcomes.append(outcome)
                session.add(ResearchOutcomeRecord(
                    id=str(uuid4()), run_id=run.id, decision_id=record.id,
                    policy_version="research_shadow_outcome_v1",
                    terminal_status=str(outcome["terminal_status"]), realized_r=outcome["realized_r"],
                    bars_held=int(outcome["bars_held"]), mfe_r=outcome["mfe_r"], mae_r=outcome["mae_r"],
                    terminal_candle_timestamp=outcome["terminal_candle_timestamp"],
                    reason_code=str(outcome["reason_code"]), evaluated_at=outcome["evaluated_at"],
                ))
            outcome_seconds = time.perf_counter() - outcome_started
            metric_summary = _outcome_metrics(outcomes, signal_count=len(signal_decisions))
            summary = {
                "eligible_candles": len(seen), "buy": counts["BUY"], "sell": counts["SELL"],
                "no_trade": counts["NO_TRADE"], "reason_counts": dict(reasons),
                "execution_allowed": False, "orders_sent": 0, "broker_writes": 0,
                "funnel": {"snapshots": len(seen), "decisions": len(signal_decisions),
                           "eligible": len(signal_decisions), "no_trade_aggregate": counts["NO_TRADE"]},
                **metric_summary,
                "timings": {"feature_calculation_seconds": canonical_seconds,
                            "signal_generation_seconds": canonical_seconds,
                            "persistence_seconds": persistence_seconds,
                            "outcome_evaluation_seconds": outcome_seconds,
                            "total_runtime_seconds": time.perf_counter() - run_started},
            }
            statistics = getattr(strategy, "research_statistics", None)
            if callable(statistics):
                summary["pair_zone_lifecycle"] = statistics()
            run.status = "COMPLETED"
            run.completed_at = datetime.now(UTC)
            run.summary_json = summary
            for key, value in summary.items():
                if isinstance(value, (int, float)):
                    session.add(ResearchMetricRecord(
                        run_id=run.id, metric_key=key, value=float(value),
                        denominator=len(seen), dimension_json={},
                    ))
            session.commit()
            outputs.append({"run_id": run_uuid, "strategy_id": strategy.metadata.strategy_id,
                            "strategy_version": strategy.metadata.strategy_version, "rr": float(rr_value),
                            "dataset_id": dataset["dataset_id"], "dataset_hash": dataset["dataset_hash"],
                            **summary, "status": run.status, "split_definition": split_definition,
                            "total_runtime_seconds": time.perf_counter() - run_started})
    if outputs:
        outputs[0]["canonical_generation_seconds"] = canonical_seconds
        outputs[0]["total_grid_runtime_seconds"] = time.perf_counter() - total_started
    return outputs


def list_runs(database, *, limit: int = 20) -> list[ResearchRunRecord]:
    with database.session() as session:
        return list(session.scalars(select(ResearchRunRecord).order_by(desc(ResearchRunRecord.created_at)).limit(limit)))


def recover_interrupted_runs(database) -> int:
    """Mark work left RUNNING/QUEUED by a process restart as INTERRUPTED."""
    changed = 0
    with database.session() as session:
        rows = session.scalars(
            select(ResearchRunRecord).where(ResearchRunRecord.status.in_(("RUNNING", "QUEUED")))
        ).all()
        for row in rows:
            row.status = "INTERRUPTED"
            row.completed_at = datetime.now(UTC)
            row.summary_json = {**(row.summary_json or {}), "interrupted_on_restart": True,
                                "execution_allowed": False}
            changed += 1
    return changed


def mark_run_failed(database, run_id: str, error_type: str) -> None:
    """Persist a bounded failure category; never persist exception secrets."""
    with database.session() as session:
        row = session.scalar(select(ResearchRunRecord).where(ResearchRunRecord.run_id == run_id))
        if row is not None:
            row.status = "FAILED"
            row.completed_at = datetime.now(UTC)
            row.summary_json = {"error": error_type[:100], "execution_allowed": False,
                                "orders_sent": 0, "broker_writes": 0}


def mark_runs_failed_by_name(database, run_name: str | None, error_type: str) -> int:
    """Bounded CLI recovery for a run that failed before its result was returned."""
    if not run_name:
        return 0
    changed = 0
    with database.session() as session:
        rows = session.scalars(
            select(ResearchRunRecord).where(
                ResearchRunRecord.run_name == run_name,
                ResearchRunRecord.status == "RUNNING",
            )
        ).all()
        for row in rows:
            row.status = "FAILED"
            row.completed_at = datetime.now(UTC)
            row.summary_json = {"error": error_type[:100], "execution_allowed": False,
                                "orders_sent": 0, "broker_writes": 0}
            changed += 1
    return changed


def enqueue_backtest_dataset(database, settings, *, strategy_id: str, project_root: Path,
                             run_name: str | None = None) -> str:
    """Create a durable QUEUED research job manifest without running it inline."""
    imported = import_research_dataset(database, timeframe="M5", symbol=settings.trading_symbol)
    strategy = StrategyRegistry(settings).resolve(strategy_id)
    git_commit, git_dirty = _git_trace(project_root)
    run_id = str(uuid4())
    with database.session() as session:
        dataset = session.scalar(select(ResearchDatasetRecord).where(ResearchDatasetRecord.dataset_id == imported.dataset_id))
        assert dataset is not None
        session.add(ResearchRunRecord(
            run_id=run_id, run_name=run_name, strategy_id=strategy.metadata.strategy_id,
            strategy_version=strategy.metadata.strategy_version, config_hash=strategy.metadata.config_hash,
            dataset_id=dataset.id, dataset_hash=dataset.dataset_hash, symbol=dataset.symbol, timeframe="M5",
            status="QUEUED", started_at=None, completed_at=None, git_commit=git_commit, git_dirty=git_dirty,
            engine_version=ENGINE_VERSION, parameters_json={"queued": True, "rr_grid": list(RR_GRID)},
            split_definition=build_split_definition(dataset.start_at, dataset.end_at), summary_json={}, execution_allowed=False,
        ))
    return run_id
