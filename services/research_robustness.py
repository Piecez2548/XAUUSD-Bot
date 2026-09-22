"""Research-only robustness, cost, and stability analysis for frozen signals.

This module consumes immutable persisted Pair Zone research runs.  It never
re-evaluates or mutates the frozen production strategy for the primary results,
and it has no broker or execution path.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import random
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import desc, select

from persistence.orm import (
    ResearchCandleRecord,
    ResearchDatasetRecord,
    ResearchDecisionRecord,
    ResearchOutcomeRecord,
    ResearchRobustnessRecord,
    ResearchRunRecord,
)
from services.shadow_outcome import EvaluationPolicy, evaluate_decision

ROBUSTNESS_ENGINE_VERSION = "robustness_engine_v1"
ROBUSTNESS_SEED = 240922
ROBUSTNESS_SIMULATIONS = 1000
FROZEN_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "strategies" / "pair_zone_v1.yaml"


@dataclass(frozen=True, slots=True)
class CostScenario:
    name: str
    spread_points: float
    entry_slippage_points: float
    exit_slippage_points: float
    commission_r: float


@dataclass(frozen=True, slots=True)
class TradeSample:
    timestamp: datetime
    side: str
    entry_price: float
    stop_loss: float
    gross_r: float | None
    terminal_status: str


COST_SCENARIOS = (
    CostScenario("zero", 0.0, 0.0, 0.0, 0.0),
    CostScenario("normal", 20.0, 0.5, 0.5, 0.02),
    CostScenario("elevated", 40.0, 1.0, 1.0, 0.04),
    CostScenario("stress", 80.0, 2.0, 2.0, 0.08),
)


def frozen_config_manifest(path: Path = FROZEN_CONFIG_PATH) -> dict[str, str]:
    """Hash exact bytes and parsed values without changing the frozen file."""
    raw = path.read_bytes()
    parsed = yaml.safe_load(raw) or {}
    canonical = yaml.safe_dump(parsed, sort_keys=True).encode("utf-8")
    return {
        "path": str(path),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "parsed_sha256": hashlib.sha256(canonical).hexdigest(),
        "strategy_id": str(parsed.get("strategy_id", "")),
        "strategy_version": str(parsed.get("strategy_version", "")),
        "config_version": str(parsed.get("config_version", "")),
    }


def cost_r(sample: TradeSample, scenario: CostScenario, *, point: float = 0.01) -> float:
    """Return adverse spread/slippage/commission measured in initial R."""
    if sample.gross_r is None:
        return 0.0
    risk = abs(sample.entry_price - sample.stop_loss)
    if risk <= 0:
        return 0.0
    price_cost = (scenario.spread_points + scenario.entry_slippage_points + scenario.exit_slippage_points) * point
    return price_cost / risk + scenario.commission_r


def adverse_slippage_prices(side: str, entry_price: float, exit_price: float, scenario: CostScenario, *, point: float = 0.01) -> tuple[float, float]:
    """Return conservative research-only entry/exit prices by side."""
    entry_slip = scenario.entry_slippage_points * point
    exit_slip = scenario.exit_slippage_points * point
    if side == "BUY":
        return entry_price + entry_slip, exit_price - exit_slip
    if side == "SELL":
        return entry_price - entry_slip, exit_price + exit_slip
    raise ValueError("side must be BUY or SELL")


def parameter_variant_config(base: dict[str, Any], parameter: str, fraction: float) -> dict[str, Any]:
    """Build an isolated diagnostic config without mutating the base mapping."""
    if parameter not in base:
        raise KeyError(parameter)
    variant = dict(base)
    value = float(base[parameter]) * (1.0 + fraction)
    variant[parameter] = int(round(value)) if parameter == "zone_max_age_minutes" else value
    return variant


def _drawdown(values: Iterable[float]) -> tuple[float, int, int]:
    equity = peak = max_dd = 0.0
    loss_streak = win_streak = max_loss = max_win = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if value < 0:
            loss_streak += 1
            win_streak = 0
        elif value > 0:
            win_streak += 1
            loss_streak = 0
        else:
            loss_streak = win_streak = 0
        max_loss = max(max_loss, loss_streak)
        max_win = max(max_win, win_streak)
    return max_dd, max_loss, max_win


def metrics(samples: list[TradeSample], scenario: CostScenario = COST_SCENARIOS[0]) -> dict[str, Any]:
    """Calculate gross and net metrics without changing terminal outcomes."""
    ordered = sorted(samples, key=lambda item: item.timestamp)
    gross = [item.gross_r for item in ordered]
    costs = [cost_r(item, scenario) for item in ordered]
    net = [value - cost if value is not None else None for value, cost in zip(gross, costs, strict=True)]
    statuses = Counter(item.terminal_status for item in ordered)
    terminal = [index for index, item in enumerate(ordered) if item.terminal_status in {"TP_HIT", "SL_HIT", "AMBIGUOUS", "EXPIRED"} and net[index] is not None]
    terminal_net = [float(net[index]) for index in terminal]
    terminal_gross = [float(gross[index]) for index in terminal]
    positive = [value for value in terminal_net if value > 0]
    negative = [value for value in terminal_net if value < 0]
    gross_dd, gross_loss, gross_win = _drawdown(terminal_gross)
    net_dd, net_loss, net_win = _drawdown(terminal_net)
    gross_total = sum(terminal_gross)
    net_total = sum(terminal_net)
    return {
        "trades": len(ordered), "tp": statuses["TP_HIT"], "sl": statuses["SL_HIT"],
        "ambiguous": statuses["AMBIGUOUS"], "expired": statuses["EXPIRED"], "pending": statuses["PENDING"],
        "coverage": len(terminal) / len(ordered) if ordered else 0.0,
        "win_rate": len(positive) / len(terminal_net) if terminal_net else None,
        "gross_total_r": gross_total, "net_total_r": net_total,
        "gross_average_r": statistics.fmean(terminal_gross) if terminal_gross else None,
        "net_average_r": statistics.fmean(terminal_net) if terminal_net else None,
        "gross_median_r": statistics.median(terminal_gross) if terminal_gross else None,
        "net_median_r": statistics.median(terminal_net) if terminal_net else None,
        "gross_profit_factor": sum(value for value in terminal_gross if value > 0) / abs(sum(value for value in terminal_gross if value < 0)) if any(value < 0 for value in terminal_gross) else None,
        "net_profit_factor": sum(positive) / abs(sum(negative)) if negative else None,
        "gross_max_drawdown_r": gross_dd, "net_max_drawdown_r": net_dd,
        "gross_maximum_consecutive_losses": gross_loss, "net_maximum_consecutive_losses": net_loss,
        "gross_maximum_consecutive_wins": gross_win, "net_maximum_consecutive_wins": net_win,
        "gross_expectancy": statistics.fmean(terminal_gross) if terminal_gross else None,
        "net_expectancy": statistics.fmean(terminal_net) if terminal_net else None,
        "cost_total_r": gross_total - net_total,
        "scenario": {"name": scenario.name, "spread_points": scenario.spread_points,
                      "entry_slippage_points": scenario.entry_slippage_points,
                      "exit_slippage_points": scenario.exit_slippage_points,
                      "commission_r": scenario.commission_r},
    }


def monthly_metrics(samples: list[TradeSample], scenario: CostScenario) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.timestamp.astimezone(UTC).strftime("%Y-%m")].append(sample)
    return {month: metrics(rows, scenario) for month, rows in sorted(grouped.items())}


def side_metrics(samples: list[TradeSample], scenario: CostScenario) -> dict[str, dict[str, Any]]:
    return {side: metrics([item for item in samples if item.side == side], scenario) for side in ("BUY", "SELL")}


def concentration_metrics(samples: list[TradeSample], scenario: CostScenario) -> dict[str, Any]:
    ordered = sorted(samples, key=lambda item: item.timestamp)
    values = [float(item.gross_r) - cost_r(item, scenario) if item.gross_r is not None else None for item in ordered]
    resolved = [value for value in values if value is not None]
    total = sum(resolved)
    by_month = defaultdict(float)
    for item, value in zip(ordered, values, strict=True):
        if value is not None:
            by_month[item.timestamp.astimezone(UTC).strftime("%Y-%m")] += value
    ranked = sorted(((index, value) for index, value in enumerate(values) if value is not None), key=lambda item: item[1], reverse=True)
    return {
        "total_net_r": total,
        "top_1_net_r": sum(value for _, value in ranked[:1]),
        "top_5_net_r": sum(value for _, value in ranked[:5]),
        "top_10_net_r": sum(value for _, value in ranked[:10]),
        "top_10_percentage_of_total": (sum(value for _, value in ranked[:10]) / total * 100.0) if total else None,
        "best_month": max(by_month.items(), key=lambda item: item[1]) if by_month else None,
        "worst_month": min(by_month.items(), key=lambda item: item[1]) if by_month else None,
        "remove_top_1_total_net_r": sum(value for _, value in ranked[1:]),
        "remove_top_5_total_net_r": sum(value for _, value in ranked[5:]),
        "remove_top_10_total_net_r": sum(value for _, value in ranked[10:]),
    }


def monte_carlo(samples: list[TradeSample], scenario: CostScenario, *, seed: int = ROBUSTNESS_SEED, simulations: int = ROBUSTNESS_SIMULATIONS) -> dict[str, Any]:
    values = [float(sample.gross_r) - cost_r(sample, scenario) for sample in sorted(samples, key=lambda item: item.timestamp) if sample.gross_r is not None]
    rng = random.Random(seed)
    max_dds: list[float] = []
    loss_streaks: list[int] = []
    totals: list[float] = []
    for _ in range(simulations):
        shuffled = values.copy()
        rng.shuffle(shuffled)
        drawdown, loss_streak, _ = _drawdown(shuffled)
        max_dds.append(drawdown)
        loss_streaks.append(loss_streak)
        totals.append(sum(shuffled))
    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        return float(sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))])
    return {"seed": seed, "simulations": simulations,
            "max_drawdown_r_percentiles": {str(int(p * 100)): percentile(max_dds, p) for p in (0.5, 0.9, 0.95, 0.99)},
            "loss_streak_percentiles": {str(int(p * 100)): percentile(loss_streaks, p) for p in (0.5, 0.9, 0.95, 0.99)},
            "ending_total_r_percentiles": {str(int(p * 100)): percentile(totals, p) for p in (0.5, 0.9, 0.95, 0.99)}}


def _samples_for_run(session, run: ResearchRunRecord) -> list[TradeSample]:
    rows = session.execute(
        select(ResearchDecisionRecord, ResearchOutcomeRecord)
        .join(ResearchOutcomeRecord, ResearchOutcomeRecord.decision_id == ResearchDecisionRecord.id)
        .where(ResearchDecisionRecord.run_id == run.id)
        .order_by(ResearchDecisionRecord.timestamp)
    ).all()
    return [TradeSample(row.timestamp, row.decision, float(row.entry_price or 0), float(row.stop_loss or 0),
                         float(outcome.realized_r) if outcome.realized_r is not None else None,
                         outcome.terminal_status)
            for row, outcome in rows if row.decision in {"BUY", "SELL"}]


def _latest_pair_runs(session) -> dict[float, ResearchRunRecord]:
    rows = session.scalars(
        select(ResearchRunRecord).where(
            ResearchRunRecord.strategy_id == "pair_zone_v1", ResearchRunRecord.status == "COMPLETED"
        ).order_by(desc(ResearchRunRecord.created_at))
    ).all()
    selected: dict[float, ResearchRunRecord] = {}
    for row in rows:
        name = row.run_name or ""
        rr = float((row.parameters_json or {}).get("rr", 0) or 0)
        if rr and rr not in selected and "chronological" not in name and not name.endswith(("-development", "-validation", "-holdout")):
            selected[rr] = row
    return selected


def _gap_summary(session, dataset: ResearchDatasetRecord, samples: list[TradeSample]) -> dict[str, Any]:
    candles = session.scalars(select(ResearchCandleRecord).where(ResearchCandleRecord.dataset_id == dataset.id).order_by(ResearchCandleRecord.timestamp)).all()
    expected = timedelta(minutes=5)
    gaps: list[tuple[datetime, datetime, int]] = []
    for left, right in zip(candles, candles[1:], strict=False):
        missing = int((right.timestamp - left.timestamp) / expected) - 1
        if missing > 0:
            gaps.append((left.timestamp, right.timestamp, missing))
    near = [sample.timestamp for sample in samples if any(abs((sample.timestamp - endpoint).total_seconds()) <= 1800 for left, right, _ in gaps for endpoint in (left, right))]
    intraday = [item for item in gaps if item[0].weekday() < 5 and item[1].weekday() < 5 and (item[1] - item[0]) <= timedelta(hours=24)]
    return {"gap_count": len(gaps), "missing_intervals": sum(item[2] for item in gaps),
            "intraday_gap_count": len(intraday), "session_or_weekend_gap_count": len(gaps) - len(intraday),
            "signals_near_gap_30m": len(near), "signals_total": len(samples),
            "synthetic_candles_added": 0, "gap_policy": "preserve_and_report"}


def _walk_forward(samples: list[TradeSample], scenario: CostScenario, start: datetime, end: datetime) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=30) - timedelta(minutes=5), end)
        subset = [item for item in samples if cursor <= item.timestamp <= window_end]
        if subset:
            result = metrics(subset, scenario)
            windows.append({"start": cursor.isoformat(), "end": window_end.isoformat(), **result})
        cursor = window_end + timedelta(minutes=5)
    return windows


def _run_parameter_diagnostics(database, settings, dataset_id: str, baseline: dict[str, Any], max_m5_rows: int | None = None) -> list[dict[str, Any]]:
    """Run isolated temporary-config diagnostics; the frozen file is never written."""
    from services.pair_zone_strategy import PairZoneV1
    from services.research_platform import _temporary_shadow_decision, load_research_snapshots

    snapshots, m5_candles = load_research_snapshots(database, settings, dataset_id=dataset_id, max_m5_rows=max_m5_rows)
    with tempfile.TemporaryDirectory(prefix="pair-zone-robustness-") as temp_dir:
        base = yaml.safe_load(FROZEN_CONFIG_PATH.read_text(encoding="utf-8"))
        definitions = {
            "zone_max_age_minutes": (-0.10, 0.10), "stop_buffer_points": (-0.10, 0.10),
            "confirmation_wick_to_body": (-0.10, 0.10), "displacement_min_points": (-0.10, 0.10),
        }
        rows: list[dict[str, Any]] = []
        for key, (low, high) in definitions.items():
            for label, fraction in (("minus_10pct", low), ("plus_10pct", high)):
                config = parameter_variant_config(base, key, fraction)
                path = Path(temp_dir) / f"{key}-{label}.yaml"
                path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
                strategy = PairZoneV1(settings, config_path=path)
                strategy.reset_research()
                counts = Counter()
                decisions: list[Any] = []
                for snapshot, snapshot_id, risk in snapshots:
                    decision = strategy.evaluate(snapshot, market_snapshot_id=snapshot_id, risk=risk, candles_are_closed=True)
                    counts[decision.decision.value] += 1
                    if decision.decision.value in {"BUY", "SELL"}:
                        decisions.append(decision)
                outcomes: list[dict[str, Any]] = []
                stamps = [row.timestamp for row in m5_candles]
                from bisect import bisect_right
                for decision in decisions:
                    index = bisect_right(stamps, decision.m5_candle_timestamp)
                    outcomes.append(evaluate_decision(_temporary_shadow_decision(decision), m5_candles[index:index + settings.shadow_outcome_horizon_bars], policy=EvaluationPolicy(horizon_bars=settings.shadow_outcome_horizon_bars)))
                values = [TradeSample(decision.m5_candle_timestamp, decision.decision.value, float(decision.entry_price), float(decision.stop_loss), float(item["realized_r"]) if item["realized_r"] is not None else None, str(item["terminal_status"])) for decision, item in zip(decisions, outcomes, strict=True)]
                rows.append({"parameter": key, "variant": label, "value": config[key], "buy": counts["BUY"], "sell": counts["SELL"], "no_trade": counts["NO_TRADE"], "rr2_metrics": metrics(values)})
        return [{"parameter": "frozen_baseline", "variant": "0pct", "value": "frozen", "buy": baseline["buy"], "sell": baseline["sell"], "no_trade": baseline["no_trade"], "rr2_metrics": baseline}, *rows]


def run_robustness(database, settings, *, strategy_id: str = "pair_zone_v1", dataset_id: str | None = None,
                   seed: int = ROBUSTNESS_SEED, simulations: int = ROBUSTNESS_SIMULATIONS,
                   project_root: Path | None = None, run_parameter_diagnostics: bool = True,
                   max_m5_rows: int | None = None) -> dict[str, Any]:
    if strategy_id != "pair_zone_v1":
        raise ValueError("Phase 2.4 robustness is frozen to pair_zone_v1")
    manifest = frozen_config_manifest()
    with database.session() as session:
        runs = _latest_pair_runs(session)
        if 2.0 not in runs:
            raise ValueError("completed pair_zone_v1 RR2.0 run not found")
        source = runs[2.0]
        dataset = session.scalar(select(ResearchDatasetRecord).where(ResearchDatasetRecord.id == source.dataset_id))
        if dataset is None:
            raise ValueError("source dataset not found")
        primary = _samples_for_run(session, source)
        source_summary = source.summary_json or {}
        scenario_results = {scenario.name: metrics(primary, scenario) for scenario in COST_SCENARIOS}
        cost_sensitivity = {str(rr): {scenario.name: metrics(_samples_for_run(session, run), scenario) for scenario in COST_SCENARIOS} for rr, run in sorted(runs.items()) if 1.0 <= rr <= 3.0}
        monthly = monthly_metrics(primary, COST_SCENARIOS[1])
        sides = side_metrics(primary, COST_SCENARIOS[1])
        concentration = concentration_metrics(primary, COST_SCENARIOS[1])
        simulation = monte_carlo(primary, COST_SCENARIOS[1], seed=seed, simulations=simulations)
        expired = [item for item in primary if item.terminal_status == "EXPIRED"]
        expired_values = [float(item.gross_r) for item in expired if item.gross_r is not None]
        all_values = [float(item.gross_r) for item in primary if item.gross_r is not None]
        non_expired_values = [float(item.gross_r) for item in primary if item.terminal_status != "EXPIRED" and item.gross_r is not None]
        expired_summary = {"count": len(expired), "share": len(expired) / len(primary) if primary else 0.0,
                           "average_gross_r": statistics.fmean(expired_values) if expired_values else None,
                           "median_gross_r": statistics.median(expired_values) if expired_values else None,
                           "including_expired_total_gross_r": sum(all_values),
                           "excluding_expired_total_gross_r": sum(non_expired_values)}
        walk_forward = _walk_forward(primary, COST_SCENARIOS[1], dataset.start_at, dataset.end_at)
        gaps = _gap_summary(session, dataset, primary)
        config_match = source.config_hash == str(source_summary.get("config_hash", source.config_hash))
        summary = {
            "acceptance": "ROBUSTNESS TESTS COMPLETE",
            "classification": "positive" if scenario_results["normal"]["net_total_r"] > 0 else ("negative" if scenario_results["normal"]["net_total_r"] < 0 else "neutral"),
            "strategy_id": strategy_id, "strategy_version": source.strategy_version,
            "frozen_config": {**manifest, "source_run_config_hash": source.config_hash, "source_config_match": config_match},
            "dataset": {"dataset_id": dataset.dataset_id, "dataset_hash": dataset.dataset_hash, "symbol": dataset.symbol, "timeframe": dataset.timeframe, "start_at": dataset.start_at.isoformat(), "end_at": dataset.end_at.isoformat(), "row_count": dataset.row_count},
            "canonical_signals": {"buy": int(source_summary.get("buy", 0)), "sell": int(source_summary.get("sell", 0)), "no_trade": int(source_summary.get("no_trade", 0)), "total": len(primary)},
            "cost_scenarios": scenario_results, "cost_sensitivity_rr": cost_sensitivity,
            "monthly_stability_normal_cost": monthly, "buy_sell_normal_cost": sides,
            "concentration_normal_cost": concentration, "monte_carlo_normal_cost": simulation,
            "expired_analysis": expired_summary, "walk_forward_normal_cost": walk_forward,
            "gap_sensitivity": gaps, "execution_allowed": False, "orders_sent": 0, "broker_writes": 0,
            "parameter_perturbations": [], "engine_version": ROBUSTNESS_ENGINE_VERSION,
        }
        robustness_id = f"robustness_{uuid4()}"
        record = ResearchRobustnessRecord(
            robustness_id=robustness_id, strategy_id=strategy_id, strategy_version=source.strategy_version,
            config_hash=source.config_hash, dataset_id=dataset.id, dataset_hash=dataset.dataset_hash,
            source_run_id=source.id, status="RUNNING", seed=seed, simulation_count=simulations,
            parameters_json={"cost_scenarios": [scenario.__dict__ if hasattr(scenario, "__dict__") else {"name": scenario.name, "spread_points": scenario.spread_points, "entry_slippage_points": scenario.entry_slippage_points, "exit_slippage_points": scenario.exit_slippage_points, "commission_r": scenario.commission_r} for scenario in COST_SCENARIOS], "frozen_strategy": True, "execution_allowed": False},
            summary_json=summary, execution_allowed=False,
        )
        session.add(record)
        session.flush()
        # Commit the immutable primary evidence before optional diagnostics.
        record.status = "COMPLETED"
        record.summary_json = summary
        session.commit()
        result = {"robustness_id": robustness_id, "summary": summary}
    if run_parameter_diagnostics:
        diagnostics = _run_parameter_diagnostics(
            database,
            settings,
            dataset.dataset_id,
            {**summary["cost_scenarios"]["zero"], **summary["canonical_signals"]},
            max_m5_rows=max_m5_rows,
        )
        with database.session() as session:
            row = session.scalar(select(ResearchRobustnessRecord).where(ResearchRobustnessRecord.robustness_id == robustness_id))
            if row is not None:
                row.summary_json = {**row.summary_json, "parameter_perturbations": diagnostics}
        result["summary"]["parameter_perturbations"] = diagnostics
    return result


def list_robustness(database, *, strategy_id: str = "pair_zone_v1", limit: int = 20) -> list[ResearchRobustnessRecord]:
    with database.session() as session:
        return list(session.scalars(select(ResearchRobustnessRecord).where(ResearchRobustnessRecord.strategy_id == strategy_id).order_by(desc(ResearchRobustnessRecord.created_at)).limit(limit)))
