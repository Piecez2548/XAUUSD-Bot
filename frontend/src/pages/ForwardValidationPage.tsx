import { DataState } from "../components/DataState";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useApi } from "../hooks/useApi";
import { number, utcTime } from "../lib/format";
import type {
  ForwardHealth,
  ForwardPerformance,
  ForwardSession,
  ForwardTrade,
  ResearchRobustness,
  ServiceState,
} from "../types";

function metric(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toFixed(4);
}

function sessionLabel(session: ForwardSession | null): string {
  return session?.session_id ?? "No forward session started";
}

export function ForwardValidationPage() {
  const session = useApi<ForwardSession | null>("/api/forward/session", 15_000);
  const health = useApi<ForwardHealth>("/api/forward/health", 10_000);
  const performance = useApi<ForwardPerformance>("/api/forward/performance", 15_000);
  const trades = useApi<ForwardTrade[]>("/api/forward/trades?limit=20", 15_000);
  const robustness = useApi<ResearchRobustness[]>(
    "/api/research/robustness?strategy_id=pair_zone_v1&limit=1",
    30_000,
  );
  const currentSession = session.data ?? performance.data?.session ?? null;
  const workerState = (health.data?.state ?? "UNKNOWN") as ServiceState;
  const combined = performance.data?.combined;
  const historicalSummary = robustness.data?.[0]?.summary ?? {};
  const historicalCosts = (historicalSummary.cost_scenarios ?? {}) as Record<string, Record<string, unknown>>;

  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <div className="title-row">
            <h1>Forward Validation</h1>
            <span className="page-context">LIVE FORWARD SHADOW</span>
          </div>
          <p>Forward-only Pair Zone evidence from closed broker candles. Historical research is reference-only and never merged.</p>
        </div>
        <StatusPill label="Worker" state={workerState} />
      </div>

      <div className="summary-grid">
        <div className="summary-card"><span>Execution</span><strong>DISABLED</strong></div>
        <div className="summary-card"><span>Real orders</span><strong>NONE</strong></div>
        <div className="summary-card"><span>Worker</span><strong>{workerState}</strong></div>
        <div className="summary-card"><span>Forward signals</span><strong>{performance.data?.signals ?? 0}</strong></div>
        <div className="summary-card"><span>Open virtual</span><strong>{performance.data?.OPEN ?? 0}</strong></div>
        <div className="summary-card"><span>Milestone</span><strong>{[30, 50, 100, 200].find((value) => (performance.data?.signals ?? 0) < value) ?? "200+"}</strong></div>
      </div>

      <div className="detail-grid">
        <Panel title="Session manifest" kicker="Durable activation boundary">
          {currentSession ? <div className="metrics">
            <div className="metric-row"><span>Session</span><strong className="mono">{sessionLabel(currentSession)}</strong></div>
            <div className="metric-row"><span>Strategy</span><strong>{currentSession.strategy_id} · v{currentSession.strategy_version}</strong></div>
            <div className="metric-row"><span>Config hash</span><strong className="mono">{currentSession.strategy_config_hash}</strong></div>
            <div className="metric-row"><span>Activated UTC</span><strong>{utcTime(currentSession.started_at, true)}</strong></div>
            <div className="metric-row"><span>Source / candles</span><strong>{currentSession.source_identity} · {currentSession.timeframes.join(" / ")}</strong></div>
            <div className="metric-row"><span>RR / status</span><strong>{number(currentSession.rr)} · {currentSession.status}</strong></div>
          </div> : <DataState loading={session.loading} error={session.error} emptyTitle="Forward session not started" emptyDetail="Enable FORWARD_SHADOW_ENABLED for a future live session; no historical signal is backfilled." />}
        </Panel>

        <Panel title="Authoritative worker health" kicker="Persisted heartbeat">
          {health.data ? <div className="metrics">
            <div className="metric-row"><span>State</span><strong>{health.data.state}</strong></div>
            <div className="metric-row"><span>Heartbeat age</span><strong>{health.data.age_seconds == null ? "—" : `${number(health.data.age_seconds)}s`}</strong></div>
            <div className="metric-row"><span>Closed M5 / M15 / H1</span><strong>{utcTime(health.data.last_closed_m5, true)} / {utcTime(health.data.last_closed_m15, true)} / {utcTime(health.data.last_closed_h1, true)}</strong></div>
            <div className="metric-row"><span>Last zone / signal</span><strong>{utcTime(health.data.last_zone_created, true)} / {utcTime(health.data.last_signal, true)}</strong></div>
            <div className="metric-row"><span>Failures / open virtual</span><strong>{health.data.failure_count ?? 0} / {health.data.open_shadow_trades ?? 0}</strong></div>
          </div> : <DataState loading={health.loading} error={health.error} emptyTitle="Forward health unavailable" emptyDetail="No authoritative forward worker heartbeat is available." />}
        </Panel>
      </div>

      <div className="detail-grid">
        <Panel title="Forward performance" kicker="Resolved virtual outcomes only">
          {performance.data ? <div className="metrics">
            <div className="metric-row"><span>BUY / SELL</span><strong>{performance.data.BUY} / {performance.data.SELL}</strong></div>
            <div className="metric-row"><span>TP / SL / ambiguous / expired</span><strong>{performance.data.TP} / {performance.data.SL} / {performance.data.AMBIGUOUS} / {performance.data.EXPIRED}</strong></div>
            <div className="metric-row"><span>Gross / net total R</span><strong>{metric(combined?.gross_total_r)} / {metric(combined?.net_total_r)}</strong></div>
            <div className="metric-row"><span>Net expectancy / profit factor</span><strong>{metric(combined?.net_expectancy)} / {metric(combined?.profit_factor)}</strong></div>
            <div className="metric-row"><span>Max drawdown R</span><strong>{metric(combined?.max_drawdown_r)}</strong></div>
            <div className="metric-row"><span>TP/SL only vs expired net R</span><strong>{metric(performance.data.tp_sl_only?.net_total_r)} / {metric(performance.data.expired_only?.net_total_r)}</strong></div>
          </div> : <DataState loading={performance.loading} error={performance.error} emptyTitle="No forward outcomes" emptyDetail="Signals and virtual outcomes appear only after the activation boundary." />}
        </Panel>

        <Panel title="Cost observations" kicker="Actual spread or explicit fallback">
          {combined ? <div className="metrics">
            <div className="metric-row"><span>Average observed spread</span><strong>{metric(combined.average_spread)} points</strong></div>
            <div className="metric-row"><span>Average cost</span><strong>{metric(combined.average_cost_r)} R</strong></div>
            <div className="metric-row"><span>Entry / exit slippage</span><strong>0.5 / 0.5 points</strong></div>
            <div className="metric-row"><span>Commission</span><strong>0.02 R</strong></div>
            <div className="metric-row"><span>Expired mark / MFE / MAE</span><strong>Persisted per virtual trade</strong></div>
          </div> : <DataState loading={performance.loading} error={performance.error} emptyTitle="Cost evidence pending" emptyDetail="Cost accounting starts with the first forward signal." />}
        </Panel>
      </div>

      <Panel title="Historical reference only" kicker="Phase 2.4 evidence is not aggregated with forward results">
        {robustness.data?.[0] ? <div className="metrics">
          <div className="metric-row"><span>Historical dataset / run</span><strong>{String(historicalSummary.dataset_id ?? "—")}</strong></div>
          <div className="metric-row"><span>Historical normal net R</span><strong>{String(historicalCosts.normal?.net_total_r ?? "—")}</strong></div>
          <div className="metric-row"><span>Historical classification</span><strong>{String(historicalSummary.classification ?? "—")}</strong></div>
          <div className="metric-row"><span>Boundary</span><strong>Separate persisted forward session</strong></div>
        </div> : <DataState loading={robustness.loading} error={robustness.error} emptyTitle="Historical reference unavailable" emptyDetail="The forward page does not infer historical metrics." />}
      </Panel>

      <Panel title="Recent forward virtual trades" kicker="No broker writes">
        {trades.data?.length ? trades.data.map((trade) => <div className="metric-row" key={trade.trade_id}><span>{utcTime(trade.timestamp, true)} · {trade.side} · {trade.state}</span><strong>gross {metric(trade.gross_r)}R · net {metric(trade.net_r)}R</strong></div>) : <DataState loading={trades.loading} error={trades.error} emptyTitle="No forward trades" emptyDetail="No BUY or SELL signal has been generated after activation." />}
      </Panel>

      <div className="safety-note"><strong>EXECUTION DISABLED — NO REAL ORDERS</strong><span>Forward validation is read-only. Pair Zone configuration is frozen; this page does not tune strategy thresholds or merge historical and live samples.</span></div>
    </div>
  );
}
