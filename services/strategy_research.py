"""Causal, isolated research runner for registered shadow strategies."""
# ruff: noqa: E501

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from models.shadow import ShadowAction
from persistence.orm import CandleRecord, SymbolRecord
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_replay import load_persisted_snapshots
from services.strategy_platform import StrategyRegistry


@dataclass(frozen=True, slots=True)
class ResearchResult:
    strategy_id: str
    strategy_version: str
    config_version: str
    config_hash: str
    dataset_start: str | None
    dataset_end: str | None
    eligible_candles: int
    buy: int
    sell: int
    no_trade: int
    reason_counts: dict[str, int]
    regime_counts: dict[str, int]
    execution_allowed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_strategy_research(database, settings, *, strategy_id: str | None = None,
                          from_time: datetime | None = None,
                          to_time: datetime | None = None) -> ResearchResult:
    identifier = strategy_id or settings.shadow_strategy
    strategy = StrategyRegistry(settings).resolve(identifier)
    snapshots = load_persisted_snapshots(database, limit=None)
    ordered = sorted(
        snapshots,
        key=lambda item: ShadowDecisionEngine._m5_timestamp(item[0], candles_are_closed=True),
    )
    seen: set[tuple[str, datetime, str]] = set()
    reasons: Counter[str] = Counter()
    regimes: Counter[str] = Counter()
    counts = Counter()
    timestamps: list[datetime] = []
    for snapshot, snapshot_id, risk in ordered:
        timestamp = ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=True)
        if from_time and timestamp < from_time or to_time and timestamp > to_time:
            continue
        key = (snapshot.symbol.name, timestamp, strategy.metadata.strategy_version)
        if key in seen:
            continue
        seen.add(key)
        decision = strategy.evaluate(
            snapshot, market_snapshot_id=snapshot_id, risk=risk, candles_are_closed=True
        )
        counts[decision.decision.value] += 1
        reasons[(decision.reason_codes or ("NONE",))[0]] += 1
        regimes[decision.market_regime.value] += 1
        timestamps.append(timestamp)
    return ResearchResult(
        strategy_id=strategy.metadata.strategy_id,
        strategy_version=strategy.metadata.strategy_version,
        config_version=strategy.metadata.config_version,
        config_hash=strategy.metadata.config_hash,
        dataset_start=timestamps[0].isoformat() if timestamps else None,
        dataset_end=timestamps[-1].isoformat() if timestamps else None,
        eligible_candles=len(seen),
        buy=counts[ShadowAction.BUY.value], sell=counts[ShadowAction.SELL.value],
        no_trade=counts[ShadowAction.NO_TRADE.value], reason_counts=dict(reasons),
        regime_counts=dict(regimes), execution_allowed=False,
    )


def historical_coverage(database) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with database.session() as session:
        for timeframe in ("M5", "M15", "H1", "H4"):
            rows = session.query(CandleRecord.timestamp).join(
                SymbolRecord, CandleRecord.symbol_id == SymbolRecord.id
            ).filter(CandleRecord.timeframe == timeframe).order_by(CandleRecord.timestamp).all()
            stamps = [row[0] for row in rows]
            result[timeframe] = {
                "count": len(stamps),
                "earliest": stamps[0].isoformat() if stamps else None,
                "latest": stamps[-1].isoformat() if stamps else None,
                "duplicate_timestamps": len(stamps) - len(set(stamps)),
            }
    return result


def write_result(result: ResearchResult, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
