import { Download, ExternalLink, Info } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DataTable, type Column } from "../components/DataTable";
import { DataState } from "../components/DataState";
import { EmptyState } from "../components/EmptyState";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useApi } from "../hooks/useApi";
import { apiUrl } from "../lib/api";
import { money, number, percent, unavailable, utcTime } from "../lib/format";
import type {
  AccountCurvePoint,
  AiDecision,
  ShadowDecision,
  ShadowHealthResponse,
  ShadowOutcome,
  ShadowOutcomeHealth,
  ShadowPerformance,
  HealthResponse,
  Position,
  PublicConfig,
  SupervisorResponse,
  ServiceState,
  RiskSnapshot,
  SymbolSnapshot,
  SystemEvent,
  Trade,
  TradeEvent,
} from "../types";

function PageIntro({ eyebrow, title, detail }: { eyebrow: string; title: string; detail: string }) {
  return <div className="page-heading"><div><div className="title-row"><h1>{title}</h1><span className="page-context">{eyebrow}</span></div><p>{detail}</p></div></div>;
}

function SummaryCards({ items }: { items: Array<[string, string]> }) {
  return <div className="summary-grid">{items.map(([label, value]) => <div className="summary-card" key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>;
}

function AnalyticsChart({ title, data, xKey, valueKey, kind = "bar", loading = false, error = null }: { title: string; data: Array<Record<string, unknown>> | null; xKey: string; valueKey: string; kind?: "bar" | "line"; loading?: boolean; error?: string | null }) {
  const latest = data?.at(-1)?.[valueKey];
  return <Panel title={title}>{data?.length ? <div className="small-chart" role="img" aria-label={`${title}. ${data.length} recorded points. Latest ${valueKey.replaceAll("_", " ")}: ${latest == null ? "unavailable" : String(latest)}.`}><ResponsiveContainer width="100%" height="100%">{kind === "line" ? <LineChart data={data}><CartesianGrid stroke="#262c36" vertical={false} /><XAxis dataKey={xKey} tick={{ fill: "#98a5b5", fontSize: 9 }} axisLine={false} /><YAxis tick={{ fill: "#98a5b5", fontSize: 9 }} axisLine={false} /><Tooltip contentStyle={{ background: "#111820", border: "1px solid #303945" }} /><Line dataKey={valueKey} stroke="#64a7f2" dot={false} /></LineChart> : <BarChart data={data}><CartesianGrid stroke="#262c36" vertical={false} /><XAxis dataKey={xKey} tick={{ fill: "#98a5b5", fontSize: 9 }} axisLine={false} /><YAxis tick={{ fill: "#98a5b5", fontSize: 9 }} axisLine={false} /><Tooltip contentStyle={{ background: "#111820", border: "1px solid #303945" }} /><Bar dataKey={valueKey} fill="#64a7f2" /></BarChart>}</ResponsiveContainer></div> : <DataState loading={loading} error={error} emptyTitle="Insufficient data" emptyDetail="This visualization appears only when real completed-trade data is available." />}</Panel>;
}

export function TradesPage() {
  const trades = useApi<Trade[]>("/api/trades?limit=500");
  const [search, setSearch] = useState("");
  const [direction, setDirection] = useState("ALL");
  const [result, setResult] = useState("ALL");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const filtered = useMemo(() => (trades.data ?? []).filter((trade) => {
    const query = search.trim().toLowerCase();
    const searchable = `${trade.trade_id} ${trade.broker_ticket ?? ""} ${trade.exit_reason ?? ""}`.toLowerCase();
    const resultName = trade.net_profit == null ? "UNKNOWN" : trade.net_profit > 0 ? "WIN" : trade.net_profit < 0 ? "LOSS" : "BE";
    const timestamp = trade.entry_time ? new Date(trade.entry_time).getTime() : null;
    return (!query || searchable.includes(query))
      && (direction === "ALL" || trade.direction === direction)
      && (result === "ALL" || resultName === result)
      && (!dateFrom || (timestamp != null && timestamp >= new Date(dateFrom).getTime()))
      && (!dateTo || (timestamp != null && timestamp <= new Date(`${dateTo}T23:59:59Z`).getTime()))
      && (!model || trade.model_version?.toLowerCase().includes(model.toLowerCase()))
      && (!prompt || trade.prompt_version?.toLowerCase().includes(prompt.toLowerCase()));
  }), [dateFrom, dateTo, direction, model, prompt, result, search, trades.data]);
  const columns: Column<Trade>[] = [
    { key: "ticket", label: "Ticket", render: (row) => <Link to={`/trades/${row.trade_id}`} className="table-link">{row.broker_ticket ?? row.trade_id.slice(0, 8)}<ExternalLink size={11} /></Link> },
    { key: "entry", label: "Entry UTC", render: (row) => utcTime(row.entry_time, true) },
    { key: "side", label: "Direction", render: (row) => row.direction },
    { key: "volume", label: "Volume", align: "right", render: (row) => number(row.volume) },
    { key: "prices", label: "Entry → Exit", align: "right", render: (row) => `${number(row.entry_price)} → ${number(row.exit_price)}` },
    { key: "risk", label: "Risk", align: "right", render: (row) => percent(row.risk_percent) },
    { key: "r", label: "Result R", align: "right", render: (row) => number(row.realized_r) },
    { key: "pnl", label: "Net P/L", align: "right", render: (row) => money(row.net_profit) },
  ];
  return <div className="page"><PageIntro eyebrow="AUDITABLE TRADE LEDGER" title="Trades" detail="Search and filter persisted closed trades. No synthetic history." /><Panel title="Trade History" action={<a className="text-button" href={apiUrl("/api/export/trades.csv")}><Download size={14} />Export CSV</a>}><div className="filter-grid"><label>Search<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Ticket, ID, exit reason" /></label><label>Direction<select value={direction} onChange={(event) => setDirection(event.target.value)}><option>ALL</option><option>BUY</option><option>SELL</option></select></label><label>Result<select value={result} onChange={(event) => setResult(event.target.value)}><option>ALL</option><option>WIN</option><option>LOSS</option><option>BE</option></select></label><label>From<input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label><label>To<input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label><label>Model<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="Version" /></label><label>Prompt<input value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Version" /></label></div>{filtered.length ? <DataTable rows={filtered} columns={columns} rowKey={(row) => row.trade_id} /> : <DataState loading={trades.loading} error={trades.error} emptyTitle="No matching trades" emptyDetail="No persisted trades match the current filters." />}</Panel></div>;
}

export function TradeDetailPage() {
  const { tradeId } = useParams();
  const trade = useApi<Trade>(tradeId ? `/api/trades/${tradeId}` : "/api/trades/not-selected");
  const events = useApi<TradeEvent[]>(tradeId ? `/api/trades/${tradeId}/events` : "/api/trades/not-selected/events");
  if (!tradeId) return <div className="page"><PageIntro eyebrow="FUTURE-READY AUDIT VIEW" title="Trade Detail" detail="Lifecycle, risk, model, prompt, and market context." /><Panel title="Trade unavailable"><EmptyState title="Trade record unavailable" detail="Open a persisted trade from the Trades page." /></Panel></div>;
  if (trade.loading || trade.error || !trade.data) return <div className="page"><PageIntro eyebrow="FUTURE-READY AUDIT VIEW" title="Trade Detail" detail="Lifecycle, risk, model, prompt, and market context." /><Panel title="Trade record"><DataState loading={trade.loading} error={trade.error} emptyTitle="Trade record unavailable" emptyDetail="No persisted trade exists for this identifier." /></Panel></div>;
  return <div className="page"><PageIntro eyebrow="RECONSTRUCTABLE LIFECYCLE" title={`Trade ${trade.data.broker_ticket ? `#${trade.data.broker_ticket}` : tradeId.slice(0, 8)}`} detail="Every value below is persisted; unavailable future context remains explicit." /><SummaryCards items={[["Direction", trade.data.direction ?? unavailable], ["Net P/L", money(trade.data.net_profit)], ["Realized R", number(trade.data.realized_r)], ["Risk", percent(trade.data.risk_percent)], ["MAE", number(trade.data.mae)], ["MFE", number(trade.data.mfe)]]} /><div className="detail-grid"><Panel title="Lifecycle">{events.data?.length ? <ol className="trade-lifecycle">{events.data.map((event) => <li key={event.event_id}><time>{utcTime(event.timestamp, true)}</time><strong>{event.event_type.replaceAll("_", " ")}</strong><span>{number(event.price)} · {event.reason ?? "No reason recorded"}</span></li>)}</ol> : <DataState loading={events.loading} error={events.error} emptyTitle="No lifecycle events" emptyDetail="A final trade row exists, but no append-only lifecycle records are available." />}</Panel><Panel title="Entry and Exit"><div className="metrics"><div className="metric-row"><span>Entry</span><strong>{number(trade.data.entry_price)}</strong></div><div className="metric-row"><span>Exit</span><strong>{number(trade.data.exit_price)}</strong></div><div className="metric-row"><span>Initial SL / TP</span><strong>{number(trade.data.initial_stop_loss)} / {number(trade.data.initial_take_profit)}</strong></div><div className="metric-row"><span>Final SL / TP</span><strong>{number(trade.data.final_stop_loss)} / {number(trade.data.final_take_profit)}</strong></div><div className="metric-row"><span>Exit reason</span><strong>{trade.data.exit_reason ?? unavailable}</strong></div></div></Panel><Panel title="AI Decision"><div className="metrics"><div className="metric-row"><span>Decision reference</span><strong>{trade.data.ai_decision_id ?? unavailable}</strong></div><div className="metric-row"><span>Model version</span><strong>{trade.data.model_version ?? unavailable}</strong></div><div className="metric-row"><span>Prompt version</span><strong>{trade.data.prompt_version ?? unavailable}</strong></div><div className="metric-row"><span>Strategy version</span><strong>{trade.data.strategy_version ?? unavailable}</strong></div></div></Panel><Panel title="Market and News Context"><EmptyState title="Context unavailable" detail="Market replay and news context are planned; Phase 1.5 does not invent them." /></Panel></div></div>;
}

export function DecisionsPage() {
  const [action, setAction] = useState("ALL");
  const path = action === "ALL" ? "/api/decisions?limit=100" : `/api/decisions?limit=100&action=${action}`;
  const decisions = useApi<AiDecision[]>(path);
  const columns: Column<AiDecision>[] = [{ key: "time", label: "UTC", render: (row) => utcTime(row.timestamp, true) }, { key: "action", label: "Action", render: (row) => row.action }, { key: "confidence", label: "Confidence", align: "right", render: (row) => row.confidence == null ? unavailable : percent(row.confidence * 100) }, { key: "risk", label: "Risk proposed", align: "right", render: (row) => percent(row.requested_risk_percent) }, { key: "validation", label: "Validation", render: (row) => row.validation_status ?? unavailable }, { key: "execution", label: "Execution", render: (row) => row.execution_status ?? unavailable }, { key: "result", label: "Result", render: (row) => row.result_trade_id ?? unavailable }];
  return <div className="page"><PageIntro eyebrow="MODEL AUDIT TRAIL" title="AI Decisions" detail="All persisted decisions, including WAIT, will be auditable here." /><Panel title="Decision History" action={<div className="inline-actions"><select value={action} onChange={(event) => setAction(event.target.value)}><option>ALL</option><option>BUY</option><option>SELL</option><option>WAIT</option><option>HOLD</option><option>MODIFY</option><option>CLOSE</option></select><a className="text-button" href={apiUrl("/api/export/decisions.csv")}><Download size={14} />CSV</a><StatusPill state="PLANNED" /></div>}>{decisions.data?.length ? <DataTable rows={decisions.data} columns={columns} rowKey={(row) => row.decision_id} /> : <DataState loading={decisions.loading} error={decisions.error} emptyTitle="AI Engine planned" emptyDetail="No decisions exist and none are generated by Phase 1.5." />}</Panel></div>;
}

export function ShadowPage() {
  const latest = useApi<ShadowDecision | null>("/api/shadow/decision", 10_000);
  const history = useApi<ShadowDecision[]>("/api/shadow/decisions?limit=100", 10_000);
  const summary = useApi<Record<string, number>>("/api/shadow/summary", 10_000);
  const health = useApi<ShadowHealthResponse>("/api/shadow/health", 5_000);
  const outcomes = useApi<ShadowOutcome[]>("/api/shadow/outcomes?limit=50", 10_000);
  const outcomeHealth = useApi<ShadowOutcomeHealth>("/api/shadow/outcome-health", 5_000);
  const performance = useApi<ShadowPerformance>("/api/shadow/performance", 10_000);
  const columns: Column<ShadowDecision>[] = [
    { key: "time", label: "M5 event UTC", render: (row) => utcTime(row.m5_candle_timestamp, true) },
    { key: "decision", label: "Decision", render: (row) => row.decision },
    { key: "regime", label: "Regime", render: (row) => row.market_regime },
    { key: "strategy", label: "Strategy", render: (row) => `${row.strategy_name} ${row.strategy_version}` },
    { key: "risk", label: "Risk", align: "right", render: (row) => percent(row.approved_risk_percent) },
    { key: "reason", label: "Reason", render: (row) => row.reason_codes.join(", ") },
    { key: "execution", label: "Execution", render: () => "DISABLED" },
  ];
  return <div className="page"><PageIntro eyebrow="DETERMINISTIC SHADOW ENGINE" title="Shadow Trading" detail="Hypothetical decisions and causal outcomes from completed broker snapshots. No orders are submitted." /><SummaryCards items={[["Total", number(summary.data?.total, 0)], ["BUY", number(summary.data?.BUY, 0)], ["SELL", number(summary.data?.SELL, 0)], ["NO_TRADE", number(summary.data?.NO_TRADE, 0)], ["Pending outcomes", number(summary.data?.pending_outcomes, 0)], ["Execution", "DISABLED"]]} /><Panel title="Operational Liveness" action={<StatusPill state={health.data?.state ?? "UNKNOWN"} />}>{health.data ? <div className="metrics"><div className="metric-row"><span>Decision worker</span><strong>{health.data.state}</strong></div><div className="metric-row"><span>Outcome worker</span><strong>{outcomeHealth.data?.state ?? "UNKNOWN"}</strong></div><div className="metric-row"><span>Outcome heartbeat</span><strong>{utcTime(outcomeHealth.data?.observed_at, true)}</strong></div><div className="metric-row"><span>Latest processed M5</span><strong>{utcTime(health.data.latest_processed_m5 ?? health.data.last_processed_candle_at, true)}</strong></div><div className="metric-row"><span>Execution</span><strong>DISABLED</strong></div></div> : <DataState loading={health.loading} error={health.error} emptyTitle="Shadow worker health unavailable" emptyDetail="No authoritative shadow-worker heartbeat is available." />}</Panel><Panel title="Shadow Performance" action={<StatusPill state="DISABLED" label="READ ONLY" />}>{performance.data ? <div className="metrics"><div className="metric-row"><span>Resolved sample</span><strong>{number(performance.data.resolved_sample_size, 0)} / {number(performance.data.eligible_trades, 0)}</strong></div><div className="metric-row"><span>TP / SL / ambiguous / expired</span><strong>{performance.data.tp_hits} / {performance.data.sl_hits} / {performance.data.ambiguous} / {performance.data.expired}</strong></div><div className="metric-row"><span>Win rate / expectancy R</span><strong>{percent(performance.data.win_rate ? performance.data.win_rate * 100 : null)} / {number(performance.data.expectancy_r)}</strong></div><div className="metric-row"><span>Total R / drawdown R</span><strong>{number(performance.data.total_r)} / {number(performance.data.max_drawdown_r)}</strong></div></div> : <DataState loading={performance.loading} error={performance.error} emptyTitle="Outcome performance unavailable" emptyDetail="No authoritative outcome sample is available yet." />}</Panel><Panel title="Latest Shadow Decision" action={<StatusPill state="DISABLED" label="SHADOW ONLY" />}>{latest.data ? <div className="metrics"><div className="metric-row"><span>Decision / regime</span><strong>{latest.data.decision} · {latest.data.market_regime}</strong></div><div className="metric-row"><span>Entry / SL / TP</span><strong>{number(latest.data.entry_price)} / {number(latest.data.stop_loss)} / {number(latest.data.take_profit)}</strong></div><div className="metric-row"><span>Reason</span><strong>{latest.data.reason_codes.join(", ")}</strong></div><div className="metric-row"><span>Execution</span><strong>DISABLED — NO ORDER SENT</strong></div></div> : <DataState loading={latest.loading} error={latest.error} emptyTitle="No shadow decision" emptyDetail="No completed verified snapshot has been analysed yet." />}</Panel><Panel title="Recent Outcomes">{outcomes.data?.length ? <DataTable rows={outcomes.data} columns={[{ key: "time", label: "M5 UTC", render: (row) => utcTime(row.decision_m5_timestamp, true) }, { key: "side", label: "Side", render: (row) => row.side }, { key: "status", label: "Status", render: (row) => row.terminal_status }, { key: "r", label: "Realized R", align: "right", render: (row) => number(row.realized_r) }, { key: "mfe", label: "MFE R", align: "right", render: (row) => number(row.mfe_r) }, { key: "mae", label: "MAE R", align: "right", render: (row) => number(row.mae_r) }]} rowKey={(row) => row.outcome_id} /> : <DataState loading={outcomes.loading} error={outcomes.error} emptyTitle="No shadow outcomes" emptyDetail="Run shadow-evaluate after enough closed M5 candles are persisted." />}</Panel><Panel title="Recent Decisions">{history.data?.length ? <DataTable rows={history.data} columns={columns} rowKey={(row) => row.decision_id} /> : <DataState loading={history.loading} error={history.error} emptyTitle="No decision history" emptyDetail="Shadow decisions will appear after completed broker observations." />}</Panel></div>;
}

export function PerformancePage() {
  const summary = useApi<Record<string, number | null>>("/api/performance/summary");
  const account = useApi<AccountCurvePoint[]>("/api/performance/account-curve?display_limit=600");
  const cumulativeR = useApi<Array<Record<string, unknown>>>("/api/performance/cumulative-r");
  const pnlDay = useApi<Array<Record<string, unknown>>>("/api/performance/pnl-by-day");
  const direction = useApi<Array<Record<string, unknown>>>("/api/performance/by-direction");
  const session = useApi<Array<Record<string, unknown>>>("/api/performance/by-session");
  const confidence = useApi<Array<Record<string, unknown>>>("/api/performance/by-confidence");
  const weekday = useApi<Array<Record<string, unknown>>>("/api/performance/by-weekday");
  const hour = useApi<Array<Record<string, unknown>>>("/api/performance/by-hour");
  const monthly = useApi<Array<Record<string, unknown>>>("/api/performance/monthly");
  const total = summary.data ? summary.data.total_trades : null;
  const accountData = (account.data ?? []).map((item) => ({ ...item })) as unknown as Array<Record<string, unknown>>;
  return <div className="page"><PageIntro eyebrow="REALIZED OUTCOMES ONLY" title="Performance" detail="Metrics and charts use persisted snapshots or completed trades; insufficient samples remain unavailable." /><SummaryCards items={[["Trades", number(total, 0)], ["Win rate", percent(summary.data?.win_rate)], ["Net P/L", money(summary.data?.net_profit)], ["Expectancy R", number(summary.data?.expectancy_r)], ["Profit factor", number(summary.data?.profit_factor)], ["Max drawdown", money(summary.data?.max_drawdown)]]} /><div className="analytics-grid"><AnalyticsChart title="Equity Curve" data={accountData} xKey="timestamp" valueKey="equity" kind="line" loading={account.loading} error={account.error} /><AnalyticsChart title="Drawdown" data={accountData} xKey="timestamp" valueKey="drawdown_percent" kind="line" loading={account.loading} error={account.error} /><AnalyticsChart title="Cumulative R" data={cumulativeR.data} xKey="timestamp" valueKey="cumulative_r" kind="line" loading={cumulativeR.loading} error={cumulativeR.error} /><AnalyticsChart title="P&L by Day" data={pnlDay.data} xKey="date" valueKey="net_profit" loading={pnlDay.loading} error={pnlDay.error} /><AnalyticsChart title="BUY vs SELL" data={direction.data} xKey="segment" valueKey="net_profit" loading={direction.loading} error={direction.error} /><AnalyticsChart title="Performance by Session" data={session.data} xKey="segment" valueKey="expectancy_r" loading={session.loading} error={session.error} /><AnalyticsChart title="Confidence Calibration" data={confidence.data?.filter((item) => Number(item.trade_count) > 0) ?? null} xKey="bucket" valueKey="expectancy_r" loading={confidence.loading} error={confidence.error} /><AnalyticsChart title="Performance by Weekday" data={weekday.data} xKey="segment" valueKey="net_profit" loading={weekday.loading} error={weekday.error} /><AnalyticsChart title="Performance by Hour" data={hour.data} xKey="segment" valueKey="net_profit" loading={hour.loading} error={hour.error} /><AnalyticsChart title="Monthly Performance" data={monthly.data} xKey="segment" valueKey="net_profit" loading={monthly.loading} error={monthly.error} /></div><Panel title="Metric Integrity"><p className="prose-note"><Info size={16} />Sharpe and Sortino remain unavailable below 30 R-multiple observations. Empty metrics are not replaced with zero.</p></Panel></div>;
}

export function RiskPage() {
  const risk = useApi<RiskSnapshot | null>("/api/risk/current", 10_000);
  const config = useApi<PublicConfig>("/api/config/public");
  const riskState = risk.error ? "ERROR" : !risk.data ? "UNKNOWN" : risk.data.freshness && risk.data.freshness !== "LIVE" ? "DEGRADED" : risk.data.unbounded_positions_count ? "DEGRADED" : "CONNECTED";
  return <div className="page"><PageIntro eyebrow="EXPOSURE POLICY" title="Risk" detail="Read-only exposure calculated from broker specifications and observed stop levels." /><SummaryCards items={[["Equity", money(risk.data?.equity)], ["Open risk", percent(risk.data?.open_risk_percent)], ["Remaining", percent(risk.data?.remaining_risk_percent)], ["Aggregate maximum", percent(risk.data?.max_aggregate_risk_percent ?? config.data?.max_aggregate_risk_percent)], ["Drawdown", percent(risk.data?.drawdown_percent)], ["Margin usage", percent(risk.data?.margin_usage_percent)]]} /><Panel title="Risk by Position" action={<StatusPill state={riskState} />}>{risk.data?.risk_per_position.length ? <div className="metrics">{risk.data.risk_per_position.map((item) => <div className="metric-row" key={item.ticket}><span>Ticket <code>{item.ticket}</code></span><strong>{item.bounded_by_stop ? percent(item.risk_percent) : "Unbounded / unavailable"}</strong></div>)}</div> : <DataState loading={risk.loading} error={risk.error} emptyTitle="No open position risk" emptyDetail="No risk-bearing positions exist in the latest observation." />}</Panel><Panel title="Snapshot Integrity"><div className="metrics"><div className="metric-row"><span>Freshness</span><strong>{risk.data?.freshness ?? "UNKNOWN"}</strong></div><div className="metric-row"><span>Observed UTC</span><strong>{utcTime(risk.data?.observed_at, true)}</strong></div><div className="metric-row"><span>Snapshot ID</span><strong className="mono">{risk.data?.snapshot_id ?? unavailable}</strong></div></div></Panel><Panel title="Policy"><div className="metrics"><div className="metric-row"><span>Hard per-trade maximum</span><strong>{percent(risk.data?.max_trade_risk_percent ?? config.data?.max_trade_risk_percent)}</strong></div><div className="metric-row"><span>Hard aggregate maximum</span><strong>{percent(risk.data?.max_aggregate_risk_percent ?? config.data?.max_aggregate_risk_percent)}</strong></div><div className="metric-row"><span>Daily P/L</span><strong>{money(risk.data?.daily_pnl)}</strong></div><div className="metric-row"><span>Daily realized loss</span><strong>{money(risk.data?.daily_realized_loss)}</strong></div></div></Panel></div>;
}

export function MarketPage() {
  const symbol = useApi<SymbolSnapshot | null>("/api/symbol", 10_000);
  const positions = useApi<Position[]>("/api/positions", 10_000);
  return <div className="page"><PageIntro eyebrow="BROKER-OBSERVED MARKET" title="Market" detail="Latest persisted tick, symbol specification, and open position count." /><SummaryCards items={[["Symbol", symbol.data?.name ?? unavailable], ["Bid", number(symbol.data?.bid, symbol.data?.digits)], ["Ask", number(symbol.data?.ask, symbol.data?.digits)], ["Spread", symbol.data ? `${number(symbol.data.spread, 0)} pts` : unavailable], ["Session", symbol.data ? symbol.data.session ?? "Unknown" : unavailable], ["Open positions", positions.data ? number(positions.data.length, 0) : unavailable]]} />{(!symbol.data || !positions.data) && <Panel title="Data verification"><DataState loading={symbol.loading || positions.loading} error={symbol.error ?? positions.error} emptyTitle="Market observation unavailable" emptyDetail="Run a read-only observation to persist verified market and position data." /></Panel>}</div>;
}

export function NewsPage() { return <div className="page"><PageIntro eyebrow="ECONOMIC CONTEXT" title="News" detail="News and economic-calendar intelligence are outside Phase 1.5." /><Panel title="News Service" action={<StatusPill state="PLANNED" />}><EmptyState title="Planned integration" detail="No news feed is connected. The observatory never implies calendar coverage until verified." /></Panel></div>; }

export function HealthPage() {
  const health = useApi<HealthResponse>("/api/system/health", 10_000);
  const supervisor = useApi<SupervisorResponse>("/api/supervisor/status", 5_000);
  const events = useApi<SystemEvent[]>("/api/system/events?limit=100", 10_000);
  const errors = (events.data ?? []).filter((event) => event.severity === "ERROR" || event.severity === "CRITICAL");
  return <div className="page"><PageIntro eyebrow="SERVICE ASSURANCE" title="System Health" detail="Backend-verified states, transport state, freshness, latency records, and recent errors." /><Panel title="Services">{health.data ? <div className="health-list wide">{Object.entries(health.data.services).map(([name, state]) => <div key={name}><span>{name.replaceAll("_", " ")}</span><StatusPill state={state} /></div>)}</div> : <DataState loading={health.loading} error={health.error} emptyTitle="Health snapshot unavailable" emptyDetail="No verified component health response is available." />}</Panel><Panel title="Supervisor"><div className="health-list wide">{supervisor.data ? Object.entries(supervisor.data.components).map(([name, item]) => <div key={name}><span>{name} · PID {item.pid ?? "—"}</span><StatusPill state={item.state as ServiceState} /><small>restarts {item.restart_count}</small></div>) : <DataState loading={supervisor.loading} error={supervisor.error} emptyTitle="Supervisor unavailable" emptyDetail="No persisted process registry is available." />}</div></Panel><SummaryCards items={[["Backend uptime", health.data ? `${number(health.data.uptime_seconds / 60, 1)} min` : unavailable], ["Last market update", utcTime(health.data?.last_market_update, true)], ["WebSocket clients", health.data ? number(health.data.websocket_clients, 0) : unavailable], ["Error count", health.data ? number(health.data.error_count, 0) : unavailable], ["Checked UTC", utcTime(health.data?.checked_at)], ["Latency", health.data ? health.data.components.some((item) => item.latency_ms != null) ? "Recorded" : "Unavailable" : unavailable]]} /><Panel title="Recent Errors">{errors.length ? <div className="event-stream standalone">{errors.map((event) => <div className="event-row" key={event.event_id}><time>{utcTime(event.timestamp, true)}</time><span className={`severity ${event.severity.toLowerCase()}`}>{event.severity}</span><span>{event.source}</span><strong>{event.event_type}</strong><span>{String(event.payload.message ?? "Recorded error")}</span><code>{event.correlation_id?.slice(0, 12) ?? unavailable}</code></div>)}</div> : <DataState loading={events.loading} error={events.error} emptyTitle="No recorded errors" emptyDetail="No ERROR or CRITICAL domain events are currently stored." />}</Panel></div>;
}

export function LogsPage() {
  const events = useApi<SystemEvent[]>("/api/system/events?limit=500", 10_000);
  const [severity, setSeverity] = useState("ALL");
  const filtered = (events.data ?? []).filter((event) => severity === "ALL" || event.severity === severity);
  return <div className="page"><PageIntro eyebrow="APPEND-ONLY DOMAIN EVENTS" title="Logs" detail="Operational activity from the same event architecture used by the Overview stream." /><Panel title="System Events" action={<select value={severity} onChange={(event) => setSeverity(event.target.value)}><option>ALL</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select>}>{filtered.length ? <div className="event-stream standalone">{filtered.map((event) => <div className="event-row" key={event.event_id}><time>{utcTime(event.timestamp, true)}</time><span className={`severity ${event.severity.toLowerCase()}`}>{event.severity}</span><span>{event.source}</span><strong>{event.event_type}</strong><span>{String(event.payload.message ?? "Recorded domain event")}</span><code>{event.correlation_id?.slice(0, 12) ?? unavailable}</code></div>)}</div> : <DataState loading={events.loading} error={events.error} emptyTitle="No matching system events" emptyDetail="No domain events match this severity." />}</Panel></div>;
}

export function SettingsPage() {
  const config = useApi<PublicConfig>("/api/config/public");
  return <div className="page"><PageIntro eyebrow="SAFE PUBLIC CONFIGURATION" title="Settings" detail="Non-secret runtime policy. Secrets are never returned by the API." /><Panel title="Runtime Policy">{config.data ? <div className="metrics"><div className="metric-row"><span>Operating mode</span><strong>READ ONLY</strong></div><div className="metric-row"><span>Execution</span><strong className="negative">DISABLED</strong></div><div className="metric-row"><span>Timezone</span><strong>{config.data.timezone}</strong></div><div className="metric-row"><span>Telegram configured</span><strong>{config.data.telegram_enabled ? "Enabled" : "Disabled"}</strong></div><div className="metric-row"><span>Per-trade maximum</span><strong>{percent(config.data.max_trade_risk_percent)}</strong></div><div className="metric-row"><span>Aggregate maximum</span><strong>{percent(config.data.max_aggregate_risk_percent)}</strong></div></div> : <DataState loading={config.loading} error={config.error} emptyTitle="Runtime policy unavailable" emptyDetail="No verified public configuration is available." />}</Panel></div>;
}
