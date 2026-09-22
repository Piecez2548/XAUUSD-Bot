import { useMemo, useState } from "react";

import { DataState } from "../components/DataState";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useApi } from "../hooks/useApi";
import { utcTime } from "../lib/format";
import type { ResearchCurve, ResearchDataset, ResearchRobustness, ResearchRun } from "../types";

const strategyIds = ["pair_zone_v1", "trend_pullback_v1"] as const;

function matchesStrategy(run: ResearchRun, strategy: string) {
  return strategy === "trend_pullback_v1"
    ? run.strategy_id === "trend_pullback" || run.strategy_id === "trend_pullback_v1"
    : run.strategy_id === strategy;
}

function metric(value: unknown) {
  return typeof value === "number" ? value.toFixed(4) : "—";
}

export function ResearchPage() {
  const [selectedStrategy, setSelectedStrategy] = useState<string>("pair_zone_v1");
  const datasets = useApi<ResearchDataset[]>("/api/research/datasets", 15_000);
  const runs = useApi<ResearchRun[]>("/api/research/runs?limit=200", 10_000);
  const comparison = useApi<ResearchRun[]>(
    `/api/research/compare?strategy_id=${encodeURIComponent(selectedStrategy)}&rr=2.0`, 10_000,
  );
  const selectedRun = comparison.data?.[0] ?? null;
  const robustness = useApi<ResearchRobustness[]>(
    `/api/research/robustness?strategy_id=${encodeURIComponent(selectedStrategy)}&limit=1`, 15_000,
  );
  const curve = useApi<ResearchCurve>(
    selectedRun ? `/api/research/runs/${selectedRun.run_id}/curve` : "/api/research/runs?limit=1",
    15_000,
  );
  const strategyRuns = useMemo(
    () => (runs.data ?? []).filter((run) => matchesStrategy(run, selectedStrategy)),
    [runs.data, selectedStrategy],
  );
  const isSplitRun = (run: ResearchRun) => ["-development", "-validation", "-holdout"].some((suffix) => run.run_name?.endsWith(suffix));
  const rrRuns = strategyRuns.filter((run) => run.parameters?.rr !== undefined && !isSplitRun(run));
  const splitRuns = strategyRuns.filter(isSplitRun);
  const summary = selectedRun?.summary ?? {};
  const funnel = (summary.funnel ?? {}) as Record<string, unknown>;
  const lifecycle = (summary.pair_zone_lifecycle ?? {}) as Record<string, unknown>;
  const reasons = (summary.reason_counts ?? {}) as Record<string, unknown>;
  const latestEquity = curve.data?.equity.at(-1)?.value;
  const latestDrawdown = curve.data?.drawdown.at(-1)?.value;
  const robustnessSummary = robustness.data?.[0]?.summary ?? {};
  const robustnessCosts = (robustnessSummary.cost_scenarios ?? {}) as Record<string, Record<string, unknown>>;
  const robustnessSides = (robustnessSummary.buy_sell_normal_cost ?? {}) as Record<string, Record<string, unknown>>;

  return <div className="page">
    <div className="page-heading"><div><div className="title-row"><h1>Strategy Lab</h1><span className="page-context">OFFLINE / SHADOW ONLY</span></div><p>Read-only evidence for independently persisted strategies. Research never sends broker orders.</p></div></div>
    <div className="summary-grid">
      <div className="summary-card"><span>Execution</span><strong>DISABLED</strong></div>
      <div className="summary-card"><span>Broker writes</span><strong>NONE</strong></div>
      <div className="summary-card"><span>Selected strategy</span><strong>{selectedStrategy}</strong></div>
      <div className="summary-card"><span>Dataset</span><strong>{selectedRun?.dataset_id ?? "—"}</strong></div>
    </div>
    <Panel title="Strategy selector" kicker="Evidence only">
      <label className="metric-row"><span>Strategy</span><select value={selectedStrategy} onChange={(event) => setSelectedStrategy(event.target.value)}>{strategyIds.map((id) => <option key={id}>{id}</option>)}</select></label>
      <div className="metric-row"><span>Version</span><strong>{selectedRun?.strategy_version ?? "—"}</strong></div>
      <div className="metric-row"><span>Dataset hash</span><strong>{selectedRun?.dataset_hash ?? "—"}</strong></div>
    </Panel>
    <Panel title="Research funnel" kicker="Canonical signals and aggregate rejections">
      <DataState loading={comparison.loading} error={comparison.error} emptyTitle="No persisted run" emptyDetail="Run the read-only research CLI or Telegram backtest command." />
      {selectedRun && <><div className="metric-row"><span>Snapshots</span><strong>{String(funnel.snapshots ?? summary.eligible_candles ?? "—")}</strong></div><div className="metric-row"><span>BUY / SELL / NO_TRADE</span><strong>{String(summary.buy ?? 0)} / {String(summary.sell ?? 0)} / {String(summary.no_trade ?? 0)}</strong></div>{Object.entries(reasons).map(([reason, count]) => <div className="metric-row" key={reason}><span>{reason}</span><strong>{String(count)}</strong></div>)}</>}
    </Panel>
    <Panel title="RR results" kicker="Same canonical signal set">
      {rrRuns.map((run) => <div className="metric-row" key={run.run_id}><span>RR {String(run.parameters?.rr)} · {run.run_id.slice(0, 8)}</span><strong>Trades {String(run.summary?.trades ?? 0)} · Avg R {metric(run.summary?.average_r)} · Total R {metric(run.summary?.total_r)} · PF {metric(run.summary?.profit_factor)}</strong></div>)}
    </Panel>
    <Panel title="Chronological validation" kicker="Development · Validation · Holdout">
      {splitRuns.map((run) => <div className="metric-row" key={run.run_id}><span>{run.run_name?.replace("phase-2.3-pair-zone-", "") ?? run.run_id.slice(0, 8)}</span><strong>Trades {String(run.summary?.trades ?? 0)} · Total R {metric(run.summary?.total_r)} · Win {metric(run.summary?.win_rate)}</strong></div>)}
    </Panel>
    <Panel title="Pair Zone lifecycle" kicker="Persisted aggregate statistics">
      {Object.entries(lifecycle).filter(([key]) => key !== "lifecycle_states").map(([key, value]) => <div className="metric-row" key={key}><span>{key.replaceAll("_", " ")}</span><strong>{String(value)}</strong></div>)}
    </Panel>
    <Panel title="Equity and drawdown curves" kicker="Resolved outcomes in chronological order">
      <div className="metric-row"><span>Latest cumulative R</span><strong>{metric(latestEquity)}</strong></div><div className="metric-row"><span>Latest drawdown R</span><strong>{metric(latestDrawdown)}</strong></div><div className="metric-row"><span>Curve points</span><strong>{curve.data?.equity.length ?? 0}</strong></div>
      <div className="metric-row"><span>Equity curve (latest)</span><strong>{curve.data?.equity.slice(-8).map((point) => point.value.toFixed(2)).join(" → ") || "—"}</strong></div>
      <div className="metric-row"><span>Drawdown curve (latest)</span><strong>{curve.data?.drawdown.slice(-8).map((point) => point.value.toFixed(2)).join(" → ") || "—"}</strong></div>
    </Panel>
    <Panel title="Robustness and cost validation" kicker="Frozen signals · descriptive evidence only">
      <DataState loading={robustness.loading} error={robustness.error} emptyTitle="No robustness run" emptyDetail="Run the read-only Phase 2.4 research analysis." />
      {robustness.data?.[0] && <>
        <div className="metric-row"><span>Acceptance</span><strong>{String(robustnessSummary.acceptance ?? "—")}</strong></div>
        <div className="metric-row"><span>Classification</span><strong>{String(robustnessSummary.classification ?? "—")}</strong></div>
        {(["zero", "normal", "elevated", "stress"] as const).map((name) => <div className="metric-row" key={name}><span>{name} cost net R</span><strong>{metric(robustnessCosts[name]?.net_total_r)}</strong></div>)}
        <div className="metric-row"><span>BUY / SELL net R</span><strong>{metric(robustnessSides.BUY?.net_total_r)} / {metric(robustnessSides.SELL?.net_total_r)}</strong></div>
        <div className="metric-row"><span>Monte Carlo</span><strong>seed {String(robustness.data[0].seed)} · {String(robustness.data[0].simulation_count)} simulations</strong></div>
      </>}
    </Panel>
    <Panel title="Recent persisted runs"><DataState loading={runs.loading} error={runs.error} emptyTitle="No backtest runs" emptyDetail="Use the read-only CLI or Telegram /backtest." />{strategyRuns.slice(0, 20).map((run) => <div className="metric-row" key={run.run_id}><span>{run.run_id.slice(0, 8)} · {run.strategy_id} · RR {String(run.parameters?.rr ?? "GRID")}</span><strong><StatusPill state={run.status === "COMPLETED" ? "CONNECTED" : run.status === "FAILED" ? "ERROR" : "STARTING"} /> {run.summary?.buy ?? 0} BUY / {run.summary?.sell ?? 0} SELL / {run.summary?.no_trade ?? 0} NO_TRADE</strong></div>)}</Panel>
    <Panel title="Research datasets">{datasets.data?.map((dataset) => <div className="metric-row" key={dataset.dataset_id}><span>{dataset.dataset_id} · {dataset.symbol} {dataset.timeframe}</span><strong>{dataset.row_count} rows · {utcTime(dataset.start_at, true)} → {utcTime(dataset.end_at, true)}</strong></div>)}</Panel>
  </div>;
}
