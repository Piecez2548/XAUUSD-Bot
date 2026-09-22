import {
  Activity,
  BarChart3,
  Bot,
  CandlestickChart,
  ChevronLeft,
  CircleGauge,
  FileClock,
  HeartPulse,
  LayoutDashboard,
  Menu,
  Newspaper,
  Settings,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { utcTime } from "../lib/format";
import type { HealthResponse } from "../types";
import { useApi } from "../hooks/useApi";
import { StatusPill } from "./StatusPill";

const nav = [
  ["/", "Overview", LayoutDashboard],
  ["/trades", "Trades", CandlestickChart],
  ["/decisions", "AI Decisions", Bot],
  ["/shadow", "Shadow Trading", Sparkles],
  ["/performance", "Performance", BarChart3],
  ["/risk", "Risk", ShieldCheck],
  ["/market", "Market", CircleGauge],
  ["/news", "News", Newspaper],
  ["/health", "System Health", HeartPulse],
  ["/logs", "Logs", FileClock],
  ["/settings", "Settings", Settings],
] as const;

export function AppShell() {
  const [compact, setCompact] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [clock, setClock] = useState(new Date().toISOString());
  const health = useApi<HealthResponse>("/api/system/health", 10_000);
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date().toISOString()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const services = health.data?.services;
  return (
    <div className={`app-shell ${compact ? "sidebar-compact" : ""}`}>
      <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true"><Activity size={18} /></div>
          <div className="brand-copy">
            <strong>XAUUSD</strong><span>AI TRADER</span>
          </div>
          <button className="icon-button mobile-close" onClick={() => setMobileOpen(false)} aria-label="Close navigation"><X size={18} /></button>
        </div>
        <div className="mode-block">
          <span>OPERATING MODE</span>
          <strong>READ ONLY</strong>
          <small>Execution disabled</small>
        </div>
        <nav aria-label="Primary navigation">
          {nav.map(([to, label, Icon]) => (
            <NavLink key={to} to={to} end={to === "/"} onClick={() => setMobileOpen(false)}>
              <Icon size={17} strokeWidth={1.7} aria-hidden="true" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <button className="collapse-button" onClick={() => setCompact((value) => !value)} aria-label="Toggle compact navigation">
          <ChevronLeft size={16} aria-hidden="true" /><span>Collapse</span>
        </button>
      </aside>
      <div className="shell-main">
        <header className="topbar">
          <button className="icon-button mobile-menu" onClick={() => setMobileOpen(true)} aria-label="Open navigation"><Menu size={19} /></button>
          <div className="topbar-title"><strong>TRADING OBSERVATORY</strong><span>PHASE 2.0 SHADOW</span></div>
          <span className="mobile-safety">READ ONLY</span>
          <div className="service-strip" aria-label="Service status">
            <StatusPill label="MT5" state={services?.mt5 ?? "UNKNOWN"} />
            <StatusPill label="DB" state={health.data?.database ?? "UNKNOWN"} />
            <StatusPill label="TELEGRAM" state={services?.telegram ?? "UNKNOWN"} />
            <StatusPill label="SHADOW" state={services?.shadow_engine ?? "UNKNOWN"} />
            <StatusPill label="NEWS" state="PLANNED" />
          </div>
          <time className="utc-clock" dateTime={clock}><span>UTC</span>{utcTime(clock)}</time>
        </header>
        <main><Outlet /></main>
        <footer className="safety-footer"><span>READ-ONLY OBSERVABILITY TERMINAL</span><strong>ORDER EXECUTION DISABLED</strong></footer>
      </div>
      {mobileOpen && <button className="scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}
    </div>
  );
}
