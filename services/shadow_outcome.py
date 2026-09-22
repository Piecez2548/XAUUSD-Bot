"""Causal, deterministic evaluation of persisted shadow decisions."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from persistence.orm import CandleRecord, ShadowDecisionRecord, ShadowOutcomeRecord, SymbolRecord
from persistence.repositories import ShadowOutcomeRepository, SystemHealthRepository

OUTCOME_POLICY_VERSION = "shadow_outcome_v1"
PENDING = "PENDING"
TP_HIT = "TP_HIT"
SL_HIT = "SL_HIT"
AMBIGUOUS = "AMBIGUOUS"
EXPIRED = "EXPIRED"
INVALID = "INVALID"
TERMINAL = {TP_HIT, SL_HIT, AMBIGUOUS, EXPIRED, INVALID}


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    version: str = OUTCOME_POLICY_VERSION
    horizon_bars: int = 12


def evaluate_decision(
    decision: ShadowDecisionRecord,
    candles: list[CandleRecord],
    *,
    policy: EvaluationPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Evaluate only closed M5 candles strictly after the decision candle."""

    selected_policy = policy or EvaluationPolicy()
    evaluated_at = now or datetime.now(UTC)
    base: dict[str, object] = {
        "evaluated_at": evaluated_at,
        "reason_code": "WAITING_FOR_FUTURE_CANDLES",
        "bars_held": 0,
        "terminal_candle_timestamp": None,
        "exit_price": None,
        "realized_r": None,
        "max_favorable_excursion_price": None,
        "max_adverse_excursion_price": None,
        "mfe_r": None,
        "mae_r": None,
    }
    entry, stop, target = decision.entry_price, decision.stop_loss, decision.take_profit
    if decision.decision not in {"BUY", "SELL"}:
        base.update(terminal_status=INVALID, reason_code="NON_TRADE_DECISION")
        return base
    if not all(
        value is not None and math.isfinite(float(value)) for value in (entry, stop, target)
    ):
        base.update(terminal_status=INVALID, reason_code="INVALID_PRICE")
        return base
    assert entry is not None and stop is not None and target is not None
    risk = entry - stop if decision.decision == "BUY" else stop - entry
    target_distance = (target - entry) if decision.decision == "BUY" else (entry - target)
    if risk <= 0 or target_distance <= 0 or not math.isfinite(risk):
        base.update(terminal_status=INVALID, reason_code="INVALID_GEOMETRY")
        return base
    future = sorted(
        (candle for candle in candles if candle.timestamp > decision.m5_candle_timestamp),
        key=lambda candle: (candle.timestamp, candle.id),
    )[: selected_policy.horizon_bars]
    mfe_price = entry
    mae_price = entry
    for index, candle in enumerate(future, 1):
        if decision.decision == "BUY":
            mfe_price = max(mfe_price, candle.high)
            mae_price = min(mae_price, candle.low)
            tp = candle.high >= target
            sl = candle.low <= stop
            favourable = mfe_price - entry
            adverse = entry - mae_price
        else:
            mfe_price = min(mfe_price, candle.low)
            mae_price = max(mae_price, candle.high)
            tp = candle.low <= target
            sl = candle.high >= stop
            favourable = entry - mfe_price
            adverse = mae_price - entry
        base.update(
            bars_held=index,
            max_favorable_excursion_price=mfe_price,
            max_adverse_excursion_price=mae_price,
            mfe_r=favourable / risk,
            mae_r=adverse / risk,
        )
        if tp or sl:
            if tp and sl:
                base.update(
                    terminal_status=AMBIGUOUS,
                    terminal_candle_timestamp=candle.timestamp,
                    reason_code="SAME_CANDLE_TP_SL_ORDER_UNKNOWN",
                )
            elif tp:
                base.update(
                    terminal_status=TP_HIT,
                    terminal_candle_timestamp=candle.timestamp,
                    exit_price=target,
                    realized_r=target_distance / risk,
                    reason_code="TAKE_PROFIT_REACHED",
                )
            else:
                base.update(
                    terminal_status=SL_HIT,
                    terminal_candle_timestamp=candle.timestamp,
                    exit_price=stop,
                    realized_r=-1.0,
                    reason_code="STOP_LOSS_REACHED",
                )
            return base
    if len(future) < selected_policy.horizon_bars:
        base.update(terminal_status=PENDING, reason_code="WAITING_FOR_FUTURE_CANDLES")
        return base
    final = future[-1]
    close = final.close
    mark_r = ((close - entry) if decision.decision == "BUY" else (entry - close)) / risk
    base.update(
        terminal_status=EXPIRED,
        terminal_candle_timestamp=final.timestamp,
        exit_price=close,
        realized_r=mark_r,
        reason_code="HORIZON_EXPIRED_MARK_TO_MARKET",
    )
    return base


class ShadowOutcomeWorker:
    """Independent outcome worker; it never calls broker write APIs."""

    def __init__(self, settings, database, *, logger: logging.Logger) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger
        self.repository = ShadowOutcomeRepository(database)
        self.health = SystemHealthRepository(database)
        self.policy = EvaluationPolicy(
            horizon_bars=getattr(settings, "shadow_outcome_horizon_bars", 12)
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._failure_count = 0
        self._last_decision_seen: datetime | None = None
        self._last_outcome_evaluated: datetime | None = None
        self._last_health_write: datetime | None = None

    @property
    def state(self) -> str:
        if not self.settings.shadow_engine_enabled:
            return "DISABLED"
        if self._failure_count >= 3:
            return "ERROR"
        if self._failure_count:
            return "DEGRADED"
        return "CONNECTED" if self._task is not None else "UNKNOWN"

    def start(self) -> None:
        if not self.settings.shadow_engine_enabled:
            self._record_health("DISABLED", "Shadow outcome worker disabled", force=True)
            return
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="shadow-outcome-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def evaluate_once(self) -> dict[str, int]:
        created = pending = terminal = errors = 0
        decisions = await asyncio.to_thread(
            self.repository.unresolved_decisions, policy_version=self.policy.version
        )
        for decision in decisions:
            try:
                outcome = self.repository.ensure_pending(
                    decision, policy_version=self.policy.version
                )
                created += 1
                self._last_decision_seen = max(
                    self._last_decision_seen or decision.m5_candle_timestamp,
                    decision.m5_candle_timestamp,
                )
            except Exception as exc:
                errors += 1
                self.logger.warning("Unable to create shadow outcome (%s)", type(exc).__name__)
        outcomes = await asyncio.to_thread(
            self.repository.list, policy_version=self.policy.version, limit=2_000
        )
        for outcome in outcomes:
            if outcome.terminal_status != PENDING:
                terminal += 1
                continue
            try:
                decision = await asyncio.to_thread(self._decision, outcome.decision_id)
                if decision is None:
                    await asyncio.to_thread(
                        self.repository.update,
                        outcome.id,
                        terminal_status=INVALID,
                        reason_code="DECISION_NOT_FOUND",
                        evaluated_at=datetime.now(UTC),
                    )
                    errors += 1
                    continue
                candles = await asyncio.to_thread(self._future_candles, decision)
                values = evaluate_decision(decision, candles, policy=self.policy)
                await asyncio.to_thread(self.repository.update, outcome.id, **values)
                self._last_outcome_evaluated = outcome.decision_m5_timestamp
                if values["terminal_status"] == PENDING:
                    pending += 1
                else:
                    terminal += 1
            except Exception as exc:
                errors += 1
                self.logger.warning("Shadow outcome evaluation failed (%s)", type(exc).__name__)
        if errors:
            self._failure_count += 1
            self._last_failure_at = datetime.now(UTC)
        else:
            self._failure_count = 0
            self._last_success_at = datetime.now(UTC)
        self._record_health(
            "DEGRADED" if errors else "CONNECTED",
            "Shadow outcome evaluation completed"
            if not errors
            else "Shadow outcome evaluation had errors",
            force=True,
            error_category="EvaluationError" if errors else None,
            metadata_extra={
                "created_count": created,
                "pending_count": pending,
                "terminal_count": terminal,
            },
        )
        return {"created": created, "pending": pending, "terminal": terminal, "errors": errors}

    async def _run(self) -> None:
        self._record_health("CONNECTED", "Shadow outcome worker started", force=True)
        try:
            while not self._stop.is_set():
                await self.evaluate_once()
                await asyncio.sleep(max(1.0, self.settings.live_candle_interval_seconds))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure_count += 1
            self._last_failure_at = datetime.now(UTC)
            self._record_health("ERROR", "Shadow outcome worker stopped unexpectedly", force=True)
            self.logger.exception("Shadow outcome worker terminated: %s", type(exc).__name__)
        finally:
            if not self._stop.is_set():
                self._record_health("DEGRADED", "Shadow outcome worker stopped", force=True)

    def _decision(self, decision_id: str) -> ShadowDecisionRecord | None:
        with self.database.session() as session:
            return session.get(ShadowDecisionRecord, decision_id)

    def _future_candles(self, decision: ShadowDecisionRecord) -> list[CandleRecord]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(CandleRecord)
                    .join(SymbolRecord, CandleRecord.symbol_id == SymbolRecord.id)
                    .where(
                        SymbolRecord.name == decision.symbol,
                        CandleRecord.timeframe == "M5",
                        CandleRecord.timestamp > decision.m5_candle_timestamp,
                    )
                    .order_by(CandleRecord.timestamp, CandleRecord.id)
                    .limit(self.policy.horizon_bars)
                )
            )

    def _record_health(
        self,
        state: str,
        message: str,
        *,
        force: bool = False,
        error_category: str | None = None,
        metadata_extra: dict[str, object] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        if (
            not force
            and self._last_health_write
            and (now - self._last_health_write).total_seconds() < 5
        ):
            return
        self._last_health_write = now
        with self.database.session() as session:
            pending = int(
                session.scalar(
                    select(func.count())
                    .select_from(ShadowOutcomeRecord)
                    .where(
                        ShadowOutcomeRecord.evaluation_policy_version == self.policy.version,
                        ShadowOutcomeRecord.terminal_status == PENDING,
                    )
                )
                or 0
            )
            terminal = int(
                session.scalar(
                    select(func.count())
                    .select_from(ShadowOutcomeRecord)
                    .where(
                        ShadowOutcomeRecord.evaluation_policy_version == self.policy.version,
                        ShadowOutcomeRecord.terminal_status.in_(TERMINAL),
                    )
                )
                or 0
            )
        metadata: dict[str, object] = {
            "state": state,
            "heartbeat_at": now.isoformat(),
            "latest_decision_seen": self._iso(self._last_decision_seen),
            "latest_outcome_evaluated": self._iso(self._last_outcome_evaluated),
            "pending_count": pending,
            "terminal_count": terminal,
            "failure_count": self._failure_count,
            "error_category": error_category,
            "execution_allowed": False,
            "evaluation_policy_version": self.policy.version,
        }
        if metadata_extra:
            metadata.update(metadata_extra)
        self.health.record("worker:shadow_outcome", state, message=message, metadata=metadata)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None


def performance_summary(
    database, *, policy_version: str = OUTCOME_POLICY_VERSION
) -> dict[str, object]:
    with database.session() as session:
        decisions = list(session.scalars(select(ShadowDecisionRecord)))
        outcomes = list(
            session.scalars(
                select(ShadowOutcomeRecord).where(
                    ShadowOutcomeRecord.evaluation_policy_version == policy_version
                )
            )
        )
    no_trade = [row for row in decisions if row.decision == "NO_TRADE"]
    eligible = [row for row in outcomes if row.side in {"BUY", "SELL"}]
    resolved = [
        row
        for row in eligible
        if row.terminal_status in {TP_HIT, SL_HIT, EXPIRED} and row.realized_r is not None
    ]
    values = [float(row.realized_r) for row in resolved]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in sorted(
        resolved,
        key=lambda row: (row.terminal_candle_timestamp or row.decision_m5_timestamp, row.id),
    ):
        cumulative += float(value.realized_r)
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    reason_counts: dict[str, int] = {}
    for row in no_trade:
        for reason in row.reason_codes or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "evaluation_policy_version": policy_version,
        "total_decisions": len(decisions),
        "buy_decisions": sum(row.decision == "BUY" for row in decisions),
        "sell_decisions": sum(row.decision == "SELL" for row in decisions),
        "no_trade_decisions": len(no_trade),
        "no_trade_rate": (len(no_trade) / len(decisions)) if decisions else None,
        "eligible_trades": len(eligible),
        "pending": sum(row.terminal_status == PENDING for row in eligible),
        "tp_hits": sum(row.terminal_status == TP_HIT for row in eligible),
        "sl_hits": sum(row.terminal_status == SL_HIT for row in eligible),
        "ambiguous": sum(row.terminal_status == AMBIGUOUS for row in eligible),
        "expired": sum(row.terminal_status == EXPIRED for row in eligible),
        "invalid": sum(row.terminal_status == INVALID for row in eligible),
        "resolved_sample_size": len(values),
        "win_rate": (len(wins) / len(values)) if values else None,
        "loss_rate": (len(losses) / len(values)) if values else None,
        "average_r": (sum(values) / len(values)) if values else None,
        "median_r": (
            sorted(values)[len(values) // 2]
            if values and len(values) % 2
            else (
                (sorted(values)[len(values) // 2 - 1] + sorted(values)[len(values) // 2]) / 2
                if values
                else None
            )
        ),
        "total_r": sum(values) if values else None,
        "expectancy_r": (sum(values) / len(values)) if values else None,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else None,
        "best_trade_r": max(values) if values else None,
        "worst_trade_r": min(values) if values else None,
        "average_bars_held": (sum(row.bars_held for row in resolved) / len(resolved))
        if resolved
        else None,
        "max_drawdown_r": max_drawdown if values else None,
        "max_consecutive_wins": _streak(values, positive=True),
        "max_consecutive_losses": _streak(values, positive=False),
        "no_trade_reason_counts": reason_counts,
        "read_only": True,
        "execution_allowed": False,
    }


def _streak(values: list[float], *, positive: bool) -> int:
    best = current = 0
    for value in values:
        if (value > 0) is positive:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def performance_breakdown(
    database, *, policy_version: str = OUTCOME_POLICY_VERSION
) -> list[dict[str, object]]:
    with database.session() as session:
        rows = session.execute(
            select(ShadowOutcomeRecord, ShadowDecisionRecord)
            .join(ShadowDecisionRecord, ShadowDecisionRecord.id == ShadowOutcomeRecord.decision_id)
            .where(ShadowOutcomeRecord.evaluation_policy_version == policy_version)
        ).all()
    groups: dict[tuple[str, str, str], list[ShadowOutcomeRecord]] = {}
    for outcome, decision in rows:
        groups.setdefault(
            (decision.strategy_version, outcome.side, decision.market_regime), []
        ).append(outcome)
    result = []
    for (strategy, side, regime), outcomes in sorted(groups.items()):
        resolved = [
            row
            for row in outcomes
            if row.realized_r is not None and row.terminal_status in {TP_HIT, SL_HIT, EXPIRED}
        ]
        vals = [float(row.realized_r) for row in resolved]
        result.append(
            {
                "strategy_version": strategy,
                "side": side,
                "regime": regime,
                "n": len(outcomes),
                "resolved_n": len(vals),
                "total_r": sum(vals) if vals else None,
                "average_r": sum(vals) / len(vals) if vals else None,
                "win_rate": sum(value > 0 for value in vals) / len(vals) if vals else None,
            }
        )
    return result
