import { Activity, ArrowDownRight, ArrowUpRight, Check, CircleDot, Radio, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DataTable, type Column } from "../components/DataTable";
import { DataState } from "../components/DataState";
import { EventTable } from "../components/EventTable";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useApi } from "../hooks/useApi";
import { useLiveEvents } from "../hooks/useLiveEvents";
import { money, number, percent, unavailable, utcTime } from "../lib/format";
import { sampleCheckpoint, thaiDateTime, workerState } from "../lib/runtime";
import { useSystemHealth } from "../components/useSystemHealth";
import type {
  AccountSnapshot,
  AccountCurvePoint,
  LiveStatusResponse,
  Position,
  PositionStatus,
  PublicConfig,
  RiskSnapshot,
  ServiceState,
  SymbolSnapshot,
  SystemEvent,
  Trade,
  ForwardHealth,
  ForwardPerformance,
  ForwardSession,
} from "../types";

const ranges = ["1D", "1W", "1M", "3M", "ALL"] as const;
const timelineTypes = new Set([
  "SYSTEM_STARTED",
  "MT5_CONNECTED",
  "MT5_RECONNECTED",
  "MARKET_SNAPSHOT_CREATED",
  "ACCOUNT_SNAPSHOT_CREATED",
  "AI_DECISION_CREATED",
  "RISK_SNAPSHOT_CREATED",
  "TRADE_APPROVED",
  "TRADE_REJECTED",
  "POSITION_OPENED",
  "POSITION_MODIFIED",
  "POSITION_CLOSED",
  "SYSTEM_ERROR",
  "SYSTEM_STOPPED",
]);

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return <div className="metric-row"><span>{label}</span><strong className={tone}>{value}</strong></div>;
}

function eventDescription(event: SystemEvent): string {
  const payload = event.payload;
  for (const key of ["message", "reason", "status"]) {
    if (typeof payload[key] === "string") return payload[key] as string;
  }
  if (event.event_type === "MARKET_SNAPSHOT_CREATED" && typeof payload.symbol === "string") {
    return `${payload.symbol} snapshot persisted`;
  }
  return "Recorded domain event";
}

function filterDate(range: (typeof ranges)[number]): number {
  const days = { "1D": 1, "1W": 7, "1M": 30, "3M": 90, ALL: Infinity }[range];
  return Date.now() - days * 86_400_000;
}

export function OverviewPage() {
  const account = useApi<AccountSnapshot | null>("/api/account", 10_000);
  const symbol = useApi<SymbolSnapshot | null>("/api/symbol", 10_000);
  const positions = useApi<Position[]>("/api/positions", 10_000);
  const positionStatus = useApi<PositionStatus>("/api/positions/status", 10_000);
  const trades = useApi<Trade[]>("/api/trades?limit=8", 30_000);
  const risk = useApi<RiskSnapshot | null>("/api/risk/current", 10_000);
  const health = useSystemHealth();
  const liveStatus = useApi<LiveStatusResponse>("/api/live/status", 5_000);
  const config = useApi<PublicConfig>("/api/config/public");
  const storedEvents = useApi<SystemEvent[]>("/api/system/events?limit=150", 15_000);
  const accountCurve = useApi<AccountCurvePoint[]>("/api/performance/account-curve?display_limit=600", 30_000);
  const forwardSession = useApi<ForwardSession | null>("/api/forward/session", 15_000);
  const forwardHealth = useApi<ForwardHealth>("/api/forward/health", 10_000);
  const forwardPerformance = useApi<ForwardPerformance>("/api/forward/performance", 15_000);
  const live = useLiveEvents(storedEvents.data ?? []);
  const [range, setRange] = useState<(typeof ranges)[number]>("ALL");
  const [severity, setSeverity] = useState("ALL");
  const [source, setSource] = useState("ALL");
  const [autoScroll, setAutoScroll] = useState(true);
  const streamRef = useRef<HTMLDivElement>(null);
  const refreshHealth = health.refresh;

  useEffect(() => {
    if (live.healthRevision > 0) refreshHealth();
  }, [live.healthRevision, refreshHealth]);

  const services = health.data?.services ?? {};
  const currency = account.data?.currency ?? "USD";
  const chartData = useMemo(() => {
    const cutoff = filterDate(range);
    return (accountCurve.data ?? [])
      .filter((point) => range === "ALL" || new Date(point.timestamp).getTime() >= cutoff)
      .map((point) => ({ ...point, drawdown: point.drawdown_percent }));
  }, [accountCurve.data, range]);
  const sources = useMemo(() => [...new Set(live.events.map((event) => event.source))].sort(), [live.events]);
  const filteredEvents = live.events.filter(
    (event) => (severity === "ALL" || event.severity === severity) && (source === "ALL" || event.source === source),
  );
  const timeline = live.events.filter((event) => timelineTypes.has(event.event_type)).slice(0, 7);

  useEffect(() => {
    if (autoScroll && streamRef.current) streamRef.current.scrollTop = 0;
  }, [autoScroll, filteredEvents.length]);

  const positionColumns: Column<Position>[] = [
    { key: "ticket", label: "Ticket", render: (row) => <span className="mono">{row.broker_ticket}</span> },
    { key: "direction", label: "Side", render: (row) => <span className={row.direction === "BUY" ? "positive" : "negative"}>{row.direction}</span> },
    { key: "volume", label: "Volume", align: "right", render: (row) => number(row.volume) },
    { key: "entry", label: "Entry", align: "right", render: (row) => number(row.open_price, symbol.data?.digits ?? 2) },
    { key: "current", label: "Current", align: "right", render: (row) => number(row.current_price, symbol.data?.digits ?? 2) },
    { key: "sl", label: "SL", align: "right", render: (row) => row.stop_loss > 0 ? number(row.stop_loss, symbol.data?.digits ?? 2) : unavailable },
    { key: "tp", label: "TP", align: "right", render: (row) => row.take_profit > 0 ? number(row.take_profit, symbol.data?.digits ?? 2) : unavailable },
    { key: "pnl", label: "Current P/L", align: "right", render: (row) => <span className={row.profit >= 0 ? "positive" : "negative"}>{money(row.profit + row.swap, currency)}</span> },
  ];
  const tradeColumns: Column<Trade>[] = [
    { key: "ticket", label: "Ticket", render: (row) => <span className="mono">{row.broker_ticket ?? unavailable}</span> },
    { key: "time", label: "Exit UTC", render: (row) => <span className="mono">{utcTime(row.exit_time, true)}</span> },
    { key: "side", label: "Side", render: (row) => <span className={row.direction === "BUY" ? "positive" : "negative"}>{row.direction}</span> },
    { key: "volume", label: "Volume", align: "right", render: (row) => number(row.volume) },
    { key: "entry", label: "Entry", align: "right", render: (row) => number(row.entry_price, symbol.data?.digits ?? 2) },
    { key: "exit", label: "Exit", align: "right", render: (row) => number(row.exit_price, symbol.data?.digits ?? 2) },
    { key: "pnl", label: "Net P/L", align: "right", render: (row) => <span className={(row.net_profit ?? 0) >= 0 ? "positive" : "negative"}>{money(row.net_profit, currency)}</span> },
  ];

  const maxRisk = risk.data?.max_aggregate_risk_percent ?? config.data?.max_aggregate_risk_percent ?? 6;
  const usedRisk = risk.data?.open_risk_percent;
  const riskWidth = usedRisk == null ? 0 : Math.min(100, Math.max(0, usedRisk / maxRisk * 100));
  const forward = forwardPerformance.data;
  const forwardSessionData = forwardSession.data ?? forward?.session;

  return (
    <div className="page overview-page">
      <div className="page-heading">
        <div><div className="title-row"><h1>Overview</h1><span className="page-context">Autonomous system observability</span></div><p>Verified state, risk exposure, and audit activity from the read-only runtime.</p></div>
        <div className="heading-state"><Radio size={15} aria-hidden="true" /><span>Live transport</span><strong className={live.connected ? "positive" : "muted-text"}>{live.connected ? "CONNECTED" : "RECONNECTING"}</strong></div>
      </div>

      <div className="overview-lead-grid">
        <div className="state-stack">
          <Panel title="Account State" kicker="LATEST VERIFIED SNAPSHOT">
            {account.data ? <div className="metrics">
              <Metric label="Balance" value={money(account.data.balance, currency)} />
              <Metric label="Equity" value={money(account.data.equity, currency)} />
              <Metric label="Unrealized P/L" value={money(account.data.profit, currency)} tone={account.data.profit >= 0 ? "positive" : "negative"} />
              <Metric label="Free Margin" value={money(account.data.free_margin, currency)} />
              <Metric label="Margin Level" value={percent(account.data.margin_level)} />
            </div> : <DataState loading={account.loading} error={account.error} emptyTitle="No account snapshot" emptyDetail="Run a read-only observation to persist verified MT5 account data." />}
          </Panel>
          <Panel title="XAUUSD Market State" kicker="BROKER SYMBOL">
            {symbol.data ? <div className="market-state">
              <div className="symbol-price"><span>{symbol.data.name}</span><strong>{number(symbol.data.bid, symbol.data.digits)}</strong><small>Bid · UTC {utcTime(symbol.data.timestamp)}</small></div>
              <div className="bid-ask"><Metric label="Ask" value={number(symbol.data.ask, symbol.data.digits)} /><Metric label="Spread" value={`${number(symbol.data.spread, 0)} pts`} /><Metric label="Session" value={symbol.data.session ?? "Unknown"} /><Metric label="Market" value={symbol.data.market_status ?? "Unknown"} /></div>
            </div> : <DataState loading={symbol.loading} error={symbol.error} emptyTitle="No market snapshot" emptyDetail="No persisted XAUUSD market snapshot is available yet." />}
          </Panel>
        </div>

        <Panel title="Audit Timeline" kicker="LATEST RECORDED FLOW" className="audit-panel" action={<span className="live-label"><span />REAL EVENTS ONLY</span>}>
          {timeline.length ? <ol className="audit-timeline">
            {timeline.map((event) => <li key={event.event_id}>
              <time>{utcTime(event.timestamp)}</time><span className={`timeline-marker ${event.severity.toLowerCase()}`}><CircleDot size={14} /></span><div><strong>{event.event_type.replaceAll("_", " ")}</strong><p>{eventDescription(event)}</p><small>{event.source} · {event.correlation_id ? event.correlation_id.slice(0, 8) : "no correlation"}</small></div>
            </li>)}
          </ol> : <DataState loading={storedEvents.loading} error={storedEvents.error} emptyTitle="No audit events recorded" emptyDetail="The timeline will populate from the existing domain event architecture after an observation." />}
        </Panel>

        <div className="state-stack">
          <Panel title="System Health" kicker="TRUTHFUL SERVICE STATE">
            <div className="health-list">
              {([ ["Supervisor", workerState(services, "supervisor")], ["API", workerState(services, "live_engine")], ["MT5", workerState(services, "mt5")], ["Database", health.data?.database ?? "UNKNOWN"], ["Telegram", workerState(services, "telegram")], ["History Worker", workerState(services, "history_worker")], ["Shadow Worker", workerState(services, "shadow_worker")], ["Shadow Outcome Worker", workerState(services, "shadow_outcome_worker")], ["Forward Shadow Worker", workerState(services, "forward_shadow_worker")], ["Execution", "DISABLED"] ] as [string, ServiceState][]).map(([label, state]) => <div key={label}><span>{label}</span><StatusPill state={state} /></div>)}
            </div>
          </Panel>
          <Panel title="Live Data Engine" kicker="READ-ONLY FRESHNESS">
            {liveStatus.data ? <div className="metrics compact">
              <Metric label="Runtime" value={liveStatus.data.state} />
              {Object.entries(liveStatus.data.freshness).map(([name, value]) => <Metric key={name} label={`${name} freshness`} value={value.state} tone={value.state === "LIVE" ? "positive" : value.state === "STALE" ? "negative" : "muted-text"} />)}
            </div> : <DataState loading={liveStatus.loading} error={liveStatus.error} emptyTitle="Live engine unavailable" emptyDetail="Start python main.py live to observe continuous broker data." />}
          </Panel>
          <Panel title="Risk Budget" kicker="AGGREGATE OPEN RISK">
            <div className="risk-number"><strong>{percent(usedRisk)}</strong><span>/ {percent(maxRisk)}</span></div>
            <div className={`risk-track ${usedRisk == null ? "unknown" : ""}`} aria-label={usedRisk == null ? "Open risk unavailable" : `${usedRisk}% of ${maxRisk}% aggregate risk used`}><span style={{ width: `${riskWidth}%` }} /></div>
            {risk.loading && <p className="inline-warning"><ShieldAlert size={14} />Loading the latest verified risk snapshot.</p>}
            {risk.error && <p className="inline-warning" role="alert"><ShieldAlert size={14} />Risk API unavailable. No exposure value is being inferred.</p>}
            {!risk.loading && !risk.error && usedRisk == null && <p className="inline-warning"><ShieldAlert size={14} />Open risk unavailable{risk.data?.unbounded_positions_count ? ` — ${risk.data.unbounded_positions_count} position(s) have no bounded stop.` : "."}</p>}
            <div className="metrics compact"><Metric label="Remaining budget" value={percent(risk.data?.remaining_risk_percent)} /><Metric label="Per-trade maximum" value={percent(risk.data?.max_trade_risk_percent ?? config.data?.max_trade_risk_percent)} /><Metric label="Aggregate maximum" value={percent(maxRisk)} /><Metric label="Current drawdown" value={percent(risk.data?.drawdown_percent)} /></div>
            <p className="policy-note">The 6% ceiling is aggregate open risk. It is not a per-trade allowance.</p>
          </Panel>
        </div>
      </div>

      <Panel title="Forward Validation Summary" kicker="PERSISTED READ-ONLY EVIDENCE" action={<StatusPill label="FORWARD" state={(forwardHealth.data?.state ?? "UNKNOWN") as ServiceState} />}>
        {forward && forwardSessionData ? <div className="forward-summary-grid">
          <div className="forward-identity"><span className="panel-context">SESSION</span><strong className="mono">{forwardSessionData.session_id}</strong><span>{forwardSessionData.strategy_id} · v{forwardSessionData.strategy_version}</span><small>เริ่ม {thaiDateTime(forwardSessionData.started_at)} · ล่าสุด {thaiDateTime(forwardHealth.data?.last_closed_m5)}</small></div>
          <div className="forward-stat"><span>Signals</span><strong>{forward.signals}</strong><small>{sampleCheckpoint(forward.signals)}</small></div>
          <div className="forward-stat"><span>BUY / SELL</span><strong>{forward.BUY} / {forward.SELL}</strong><small>Open {forward.OPEN} · Resolved {forward.combined.resolved}</small></div>
          <div className="forward-stat"><span>TP / SL / AMBIGUOUS / EXPIRED</span><strong>{forward.TP} / {forward.SL} / {forward.AMBIGUOUS} / {forward.EXPIRED}</strong><small>Forward only</small></div>
          <div className="forward-stat"><span>Expectancy / PF</span><strong>{forward.combined.net_expectancy == null ? unavailable : `${number(forward.combined.net_expectancy, 4)} R`} / {forward.combined.profit_factor == null ? unavailable : number(forward.combined.profit_factor, 2)}</strong><small>Net R {number(forward.combined.net_total_r, 2)}</small></div>
        </div> : <DataState loading={forwardPerformance.loading || forwardSession.loading} error={forwardPerformance.error ?? forwardSession.error} emptyTitle="Forward data unavailable" emptyDetail="ยังไม่มีข้อมูล Forward หรือ backend ยังเชื่อมต่อไม่ได้ ระบบจะไม่สร้างข้อมูลแทน" />}
      </Panel>

      <Panel title="Equity & Drawdown" kicker="ACCOUNT SNAPSHOT HISTORY" className="chart-panel" action={<div className="range-control" aria-label="Chart range">{ranges.map((item) => <button key={item} aria-pressed={range === item} className={range === item ? "active" : ""} onClick={() => setRange(item)}>{item}</button>)}</div>}>
        {chartData.length ? <><div className="chart-legend" aria-hidden="true"><span className="equity">Equity</span><span className="balance">Balance</span><span className="drawdown">Drawdown %</span></div><div className="chart-wrap" role="img" aria-label={`Account snapshot chart with ${chartData.length} point(s). Latest equity ${money(chartData.at(-1)?.equity, currency)}, balance ${money(chartData.at(-1)?.balance, currency)}, drawdown ${percent(chartData.at(-1)?.drawdown)}.`}><ResponsiveContainer width="100%" height="100%"><ComposedChart data={chartData} margin={{ top: 10, right: 8, bottom: 2, left: 0 }}><CartesianGrid stroke="#303945" vertical={false} /><XAxis dataKey="timestamp" tickFormatter={(value: string) => utcTime(value, true)} tick={{ fill: "#98a5b5", fontSize: 10 }} axisLine={false} tickLine={false} /><YAxis yAxisId="currency" tick={{ fill: "#98a5b5", fontSize: 10 }} axisLine={false} tickLine={false} width={64} /><YAxis yAxisId="drawdown" orientation="right" unit="%" tick={{ fill: "#b88d8d", fontSize: 10 }} axisLine={false} tickLine={false} width={48} /><Tooltip contentStyle={{ background: "#111820", border: "1px solid #303945", borderRadius: 8 }} labelFormatter={(value) => `${utcTime(String(value), true)} UTC`} /><Area yAxisId="drawdown" type="monotone" dataKey="drawdown" name="Drawdown %" fill="#ef62621b" stroke="#ef6262" strokeWidth={1} /><Line yAxisId="currency" type="monotone" dataKey="equity" name="Equity" stroke="#3bc784" dot={chartData.length === 1} strokeWidth={2} /><Line yAxisId="currency" type="monotone" dataKey="balance" name="Balance" stroke="#64a7f2" dot={chartData.length === 1} strokeWidth={1} strokeDasharray="4 4" /></ComposedChart></ResponsiveContainer></div></> : <DataState loading={accountCurve.loading} error={accountCurve.error} emptyTitle="No account history" emptyDetail="Equity, balance, and drawdown appear after verified read-only account snapshots are persisted." />}
      </Panel>

      <Panel title="Event Stream" kicker="SYSTEM ACTIVITY" className="events-panel" action={<span className={`transport-state ${live.connected ? "connected" : ""}`}><Activity size={13} />{live.connected ? "WEBSOCKET LIVE" : "POLLING HISTORY"}</span>}>
        <div className="event-toolbar"><label>Severity<select value={severity} onChange={(event) => setSeverity(event.target.value)}><option>ALL</option>{["INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"].map((item) => <option key={item}>{item}</option>)}</select></label><label>Source<select value={source} onChange={(event) => setSource(event.target.value)}><option>ALL</option>{sources.map((item) => <option key={item}>{item}</option>)}</select></label><label className="check-control"><input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} /><span><Check size={12} /></span>Auto-scroll</label><small>{filteredEvents.length} recorded events</small></div>
        {filteredEvents.length ? <><span className="scroll-hint">Swipe or scroll horizontally to inspect every event field.</span><div ref={streamRef}><EventTable events={filteredEvents} description={eventDescription} /></div></> : <DataState loading={storedEvents.loading} error={storedEvents.error} emptyTitle="No events match this view" emptyDetail="Adjust filters or run a read-only observation to create audited system events." />}
      </Panel>

      <div className="table-grid">
        <Panel title="Open Positions" kicker="BROKER-OBSERVED STATE" action={<span className="count-badge">{positionStatus.data?.freshness ?? (positions.data ? "UNKNOWN" : unavailable)} · {positions.data ? positions.data.length : unavailable}</span>}>
          {positions.data?.length ? <DataTable rows={positions.data} columns={positionColumns} rowKey={(row) => row.position_id} /> : <DataState loading={positions.loading} error={positions.error} emptyTitle="No open positions" emptyDetail="No live positions are present in the latest persisted MT5 observation." />}
        </Panel>
        <Panel title="Recent Trades" kicker="PERSISTED CLOSED TRADES" action={<span className="count-badge">{trades.data ? trades.data.length : unavailable}</span>}>
          {trades.data?.length ? <DataTable rows={trades.data} columns={tradeColumns} rowKey={(row) => row.trade_id} /> : <DataState loading={trades.loading} error={trades.error} emptyTitle="No trade history" emptyDetail="The observatory will not fabricate trades; imported or observed records appear here." />}
        </Panel>
      </div>
      {(account.error || symbol.error || health.error || risk.error || positions.error || trades.error || storedEvents.error || accountCurve.error) && <div className="connection-banner" role="alert"><ArrowDownRight size={15} /><span>The backend is unavailable or incomplete. Values remain intentionally blank.</span></div>}
      {!account.error && !symbol.error && !health.error && !risk.error && !positions.error && !trades.error && !storedEvents.error && !accountCurve.error && <div className="sr-only"><ArrowUpRight />Observatory API connected.</div>}
    </div>
  );
}
