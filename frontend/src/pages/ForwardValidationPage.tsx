import { useMemo, useState } from "react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { DataState } from "../components/DataState";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useApi } from "../hooks/useApi";
import { number } from "../lib/format";
import { sampleCheckpoint, stateLabel, thaiDateTime } from "../lib/runtime";
import type { ForwardHealth, ForwardPerformance, ForwardSession, ForwardTrade, ResearchRobustness, ServiceState } from "../types";

function metric(value: number | null | undefined, digits = 4): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);
}

function emptyForward(detail = "ยังไม่มีสัญญาณ Forward ที่เข้าเงื่อนไข ระบบกำลังติดตามตลาดตามปกติ") {
  return <DataState loading={false} error={null} emptyTitle="ยังไม่มีสัญญาณ Forward" emptyDetail={detail} />;
}

export function ForwardValidationPage() {
  const session = useApi<ForwardSession | null>("/api/forward/session", 15_000);
  const health = useApi<ForwardHealth>("/api/forward/health", 10_000);
  const performance = useApi<ForwardPerformance>("/api/forward/performance", 15_000);
  const trades = useApi<ForwardTrade[]>("/api/forward/trades?limit=200", 15_000);
  const robustness = useApi<ResearchRobustness[]>("/api/research/robustness?strategy_id=pair_zone_v1&limit=1", 30_000);
  const [sideFilter, setSideFilter] = useState("ALL");
  const [stateFilter, setStateFilter] = useState("ALL");
  const currentSession = session.data ?? performance.data?.session ?? null;
  const workerState = (health.data?.state ?? "UNKNOWN") as ServiceState;
  const result = performance.data;
  const combined = result?.combined;
  const visibleTrades = useMemo(() => (trades.data ?? []).filter((trade) => (sideFilter === "ALL" || trade.side === sideFilter) && (stateFilter === "ALL" || trade.state === stateFilter)), [trades.data, sideFilter, stateFilter]);
  const curve = useMemo(() => {
    let cumulative = 0;
    let peak = 0;
    return [...(trades.data ?? [])].filter((trade) => trade.net_r != null).sort((a, b) => a.timestamp.localeCompare(b.timestamp)).map((trade) => {
      cumulative += trade.net_r ?? 0;
      peak = Math.max(peak, cumulative);
      return { timestamp: thaiDateTime(trade.timestamp), equity: Number(cumulative.toFixed(4)), drawdown: Number((cumulative - peak).toFixed(4)) };
    });
  }, [trades.data]);
  const historical = robustness.data?.[0]?.summary as Record<string, unknown> | undefined;
  const historicalMetrics = (historical?.metrics ?? historical?.summary ?? {}) as Record<string, unknown>;
  const hasTrades = (trades.data?.length ?? 0) > 0;

  return <div className="page">
    <div className="page-heading"><div><div className="title-row"><h1>Forward Validation</h1><span className="page-context">LIVE FORWARD SHADOW · READ ONLY</span></div><p>ข้อมูล Forward จากแท่งตลาดจริง แยกจาก Historical Research และไม่ส่งคำสั่งไป Broker</p></div><StatusPill label="Worker" state={workerState} /></div>
    <div className="safety-note"><strong>REAL-MONEY EXECUTION: DISABLED</strong><span>Forward Shadow เป็นการจำลองเท่านั้น — session, signals และ outcomes ถูกอ่านจาก API ที่บันทึกไว้</span></div>
    <div className="summary-grid">
      <div className="summary-card"><span>สถานะ Worker</span><strong>{stateLabel(workerState)}</strong></div>
      <div className="summary-card"><span>Signals</span><strong>{result?.signals ?? 0}</strong><small>{sampleCheckpoint(result?.signals ?? 0)}</small></div>
      <div className="summary-card"><span>BUY / SELL</span><strong>{result?.BUY ?? 0} / {result?.SELL ?? 0}</strong></div>
      <div className="summary-card"><span>OPEN / Resolved</span><strong>{result?.OPEN ?? 0} / {combined?.resolved ?? 0}</strong></div>
      <div className="summary-card"><span>Net R</span><strong>{metric(combined?.net_total_r, 2)}</strong></div>
      <div className="summary-card"><span>Heartbeat</span><strong>{health.data?.age_seconds == null ? "—" : `${number(health.data.age_seconds, 0)}s`}</strong><small>ล่าสุด {thaiDateTime(health.data?.heartbeat_at ?? health.data?.observed_at)}</small></div>
    </div>
    <div className="detail-grid">
      <Panel title="Session Overview" kicker="DURABLE ACTIVATION BOUNDARY">{currentSession ? <div className="metrics"><div className="metric-row"><span>Session</span><strong className="mono">{currentSession.session_id}</strong></div><div className="metric-row"><span>Strategy / version</span><strong>{currentSession.strategy_id} · {currentSession.strategy_version}</strong></div><div className="metric-row"><span>Config hash</span><strong className="mono">{currentSession.strategy_config_hash}</strong></div><div className="metric-row"><span>เริ่มทดสอบ</span><strong>{thaiDateTime(currentSession.started_at)} <small>UTC {currentSession.started_at}</small></strong></div><div className="metric-row"><span>Symbol / TF</span><strong>{currentSession.symbol} · {currentSession.timeframes.join(" / ")}</strong></div><div className="metric-row"><span>RR / status</span><strong>{number(currentSession.rr)} · {currentSession.status}</strong></div></div> : <DataState loading={session.loading} error={session.error} emptyTitle="ยังไม่มี Forward session" emptyDetail="ไม่มีการ backfill historical signal และจะไม่สร้าง session แทน" />}</Panel>
      <Panel title="Health & Freshness" kicker="AUTHORITATIVE HEARTBEAT">{health.data ? <div className="metrics"><div className="metric-row"><span>สถานะ</span><strong><StatusPill state={workerState} /></strong></div><div className="metric-row"><span>Heartbeat</span><strong>{thaiDateTime(health.data.heartbeat_at ?? health.data.observed_at)} · {health.data.age_seconds == null ? "—" : `${number(health.data.age_seconds, 0)}s`}</strong></div><div className="metric-row"><span>Latest M5 / M15 / H1</span><strong>{thaiDateTime(health.data.last_closed_m5)} / {thaiDateTime(health.data.last_closed_m15)} / {thaiDateTime(health.data.last_closed_h1)}</strong></div><div className="metric-row"><span>Latest signal</span><strong>{health.data.last_signal ? thaiDateTime(health.data.last_signal) : "ยังไม่มี"}</strong></div><div className="metric-row"><span>Failure count</span><strong>{health.data.failure_count ?? 0}</strong></div></div> : <DataState loading={health.loading} error={health.error} emptyTitle="Forward health unavailable" emptyDetail="ไม่มี authoritative heartbeat จึงไม่สรุปเป็น CONNECTED" />}</Panel>
    </div>
    <div className="detail-grid">
      <Panel title="Signal & Outcome Statistics" kicker="FORWARD ONLY">{result ? <div className="metrics"><div className="metric-row"><span>BUY / SELL / Total</span><strong>{result.BUY} / {result.SELL} / {result.signals}</strong></div><div className="metric-row"><span>TP / SL / AMBIGUOUS / EXPIRED</span><strong>{result.TP} / {result.SL} / {result.AMBIGUOUS} / {result.EXPIRED}</strong></div><div className="metric-row"><span>OPEN / Resolved</span><strong>{result.OPEN} / {combined?.resolved ?? 0}</strong></div><div className="metric-row"><span>Gross R / Net R</span><strong>{metric(combined?.gross_total_r, 2)} / {metric(combined?.net_total_r, 2)}</strong></div><div className="metric-row"><span>Expectancy / PF</span><strong>{metric(combined?.net_expectancy)} R / {metric(combined?.profit_factor, 2)}</strong></div><div className="metric-row"><span>Max DD R / Win rate</span><strong>{metric(combined?.max_drawdown_r, 2)} / {combined?.win_rate == null ? "—" : `${metric(combined.win_rate * 100, 2)}%`}</strong></div></div> : <DataState loading={performance.loading} error={performance.error} emptyTitle="ยังไม่มีผลลัพธ์ Forward" emptyDetail="ผลลัพธ์จะปรากฏหลังมี signal และข้อมูลปิดไม้จริง" />}</Panel>
      <Panel title="Cost Impact" kicker="PERSISTED SPREAD / SLIPPAGE / COMMISSION">{combined ? <div className="metrics"><div className="metric-row"><span>Average spread</span><strong>{metric(combined.average_spread, 2)} points</strong></div><div className="metric-row"><span>Average cost</span><strong>{metric(combined.average_cost_r, 4)} R</strong></div><div className="metric-row"><span>Entry / exit slippage</span><strong>จาก persisted trade records</strong></div><div className="metric-row"><span>Commission</span><strong>จาก persisted trade records</strong></div><div className="metric-row"><span>Source boundary</span><strong>ไม่รวม Historical</strong></div></div> : emptyForward("ยังไม่มี cost evidence จาก Forward signal")}</Panel>
    </div>
    <div className="analytics-grid">
      <Panel title="Equity Curve" kicker="CUMULATIVE NET R · NO FABRICATED POINTS"><div className="small-chart">{curve.length ? <ResponsiveContainer width="100%" height="100%"><AreaChart data={curve}><CartesianGrid stroke="#303945" vertical={false} /><XAxis dataKey="timestamp" hide /><YAxis tick={{ fill: "#98a5b5", fontSize: 10 }} /><Tooltip contentStyle={{ background: "#111820", border: "1px solid #303945" }} /><Area type="monotone" dataKey="equity" stroke="#3bc784" fill="#3bc78422" /></AreaChart></ResponsiveContainer> : emptyForward()}</div></Panel>
      <Panel title="Drawdown Curve" kicker="CUMULATIVE NET R FROM PEAK"><div className="small-chart">{curve.length ? <ResponsiveContainer width="100%" height="100%"><AreaChart data={curve}><CartesianGrid stroke="#303945" vertical={false} /><XAxis dataKey="timestamp" hide /><YAxis tick={{ fill: "#98a5b5", fontSize: 10 }} /><Tooltip contentStyle={{ background: "#111820", border: "1px solid #303945" }} /><Area type="monotone" dataKey="drawdown" stroke="#ef6262" fill="#ef626222" /></AreaChart></ResponsiveContainer> : emptyForward()}</div></Panel>
      <Panel title="Outcome Distribution" kicker="PERSISTED TERMINAL STATES"><div className="small-chart"><ResponsiveContainer width="100%" height="100%"><BarChart data={result ? [{ name: "TP", value: result.TP }, { name: "SL", value: result.SL }, { name: "AMBIGUOUS", value: result.AMBIGUOUS }, { name: "EXPIRED", value: result.EXPIRED }, { name: "OPEN", value: result.OPEN }] : []}><CartesianGrid stroke="#303945" vertical={false} /><XAxis dataKey="name" tick={{ fill: "#98a5b5", fontSize: 9 }} /><YAxis allowDecimals={false} tick={{ fill: "#98a5b5", fontSize: 10 }} /><Tooltip contentStyle={{ background: "#111820", border: "1px solid #303945" }} /><Bar dataKey="value" fill="#64a7f2" /></BarChart></ResponsiveContainer></div></Panel>
      <Panel title="BUY vs SELL" kicker="SEPARATE PERSISTED METRICS"><div className="metrics"><div className="metric-row"><span>BUY net / expectancy</span><strong>{metric(result?.BUY_metrics.net_total_r, 2)} / {metric(result?.BUY_metrics.net_expectancy)}</strong></div><div className="metric-row"><span>SELL net / expectancy</span><strong>{metric(result?.SELL_metrics.net_total_r, 2)} / {metric(result?.SELL_metrics.net_expectancy)}</strong></div><div className="metric-row"><span>BUY / SELL win rate</span><strong>{metric((result?.BUY_metrics.win_rate ?? 0) * 100, 2)}% / {metric((result?.SELL_metrics.win_rate ?? 0) * 100, 2)}%</strong></div></div></Panel>
    </div>
    <Panel title="Forward Virtual Trades" kicker="READ ONLY · PAGINATED API DATA"><div className="filter-grid"><label>Side<select value={sideFilter} onChange={(event) => setSideFilter(event.target.value)}><option>ALL</option><option>BUY</option><option>SELL</option></select></label><label>State<select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}><option>ALL</option><option>OPEN</option><option>TP</option><option>SL</option><option>AMBIGUOUS</option><option>EXPIRED</option></select></label><span className="filter-summary">แสดง {visibleTrades.length} / {trades.data?.length ?? 0} รายการ</span></div>{visibleTrades.length ? <div className="table-scroll"><table><thead><tr><th>Timestamp</th><th>Side</th><th>Entry / SL / TP</th><th>RR</th><th>Status</th><th>Gross R</th><th>Cost R</th><th>Net R</th><th>Spread</th><th>MFE / MAE</th><th>Zone</th></tr></thead><tbody>{visibleTrades.map((trade) => <tr key={trade.trade_id}><td className="mono">{thaiDateTime(trade.timestamp)}</td><td className={trade.side === "BUY" ? "positive" : "negative"}>{trade.side}</td><td className="mono">{number(trade.entry)} / {number(trade.stop)} / {number(trade.take_profit)}</td><td>{number(trade.risk_distance ? Math.abs(trade.take_profit - trade.entry) / trade.risk_distance : null)}</td><td><StatusPill state={(trade.state === "TP" || trade.state === "SL" ? trade.state : trade.state) as ServiceState} /></td><td>{metric(trade.gross_r, 2)}</td><td>{metric(trade.total_cost_r, 4)}</td><td>{metric(trade.net_r, 2)}</td><td>{trade.spread_points ?? "—"}</td><td>{metric(trade.mfe_r, 2)} / {metric(trade.mae_r, 2)}</td><td className="mono">{trade.signal_id.slice(0, 12)}</td></tr>)}</tbody></table></div> : hasTrades ? emptyForward("ไม่พบรายการตามตัวกรองที่เลือก") : emptyForward()}</Panel>
    <Panel title="Historical Reference Comparison" kicker="แยกจาก Forward เสมอ"><div className="metrics"><div className="metric-row"><span>Forward signals / expectancy / PF / net R</span><strong>{result?.signals ?? 0} / {metric(combined?.net_expectancy)} / {metric(combined?.profit_factor, 2)} / {metric(combined?.net_total_r, 2)}</strong></div><div className="metric-row"><span>Historical reference</span><strong>{historical ? String(historicalMetrics.total_r ?? historical?.net_total_r ?? "มีข้อมูล") : "ยังไม่มีข้อมูล"}</strong></div><div className="metric-row"><span>Sample checkpoints</span><strong>0 / 30 / 50 / 100 / 200+ (monitoring only)</strong></div><div className="metric-row"><span>Boundary</span><strong>Historical และ Forward ไม่ถูกรวมผลตอบแทน</strong></div></div></Panel>
    <div className="safety-note"><strong>REAL-MONEY EXECUTION: DISABLED</strong><span>ไม่มี endpoint สำหรับควบคุมระบบหรือส่ง order จาก dashboard นี้</span></div>
  </div>;
}
