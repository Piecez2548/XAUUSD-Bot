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
  FlaskConical,
  RadioTower,
  X,
  SlidersHorizontal,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { StatusPill } from "./StatusPill";
import { backendState, stateLabel, thaiDateTime, workerState } from "../lib/runtime";
import { BangkokClock } from "./BangkokClock";
import { SystemHealthProvider } from "./SystemHealthProvider";
import { useSystemHealth } from "./useSystemHealth";

const nav = [
  ["/", "Overview", LayoutDashboard],
  ["/trades", "Trades", CandlestickChart],
  ["/decisions", "AI Decisions", Bot],
  ["/shadow", "Shadow Trading", Sparkles],
  ["/research", "Strategy Research", FlaskConical],
  ["/forward", "Forward Validation", RadioTower],
  ["/performance", "Performance", BarChart3],
  ["/risk", "Risk", ShieldCheck],
  ["/market", "Market", CircleGauge],
  ["/news", "News", Newspaper],
  ["/health", "System Health", HeartPulse],
  ["/logs", "Logs", FileClock],
  ["/settings", "Settings", Settings],
  ["/control", "Operator Control", SlidersHorizontal],
] as const;

export function AppShell() {
  return <SystemHealthProvider><AppShellContent /></SystemHealthProvider>;
}

function AppShellContent() {
  const [compact, setCompact] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
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
          <RuntimeStatus />
          <BangkokClock />
        </header>
        <BackendStatusBanner />
        <main><Outlet /></main>
        <footer className="safety-footer"><span>READ-ONLY OBSERVABILITY TERMINAL</span><strong>ORDER EXECUTION DISABLED</strong></footer>
      </div>
      {mobileOpen && <button className="scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}
    </div>
  );
}

function RuntimeStatus() {
  const health = useSystemHealth();
  const services = health.data?.services;
  return <div className="service-strip" aria-label="Service status">
    <StatusPill label="MT5" state={workerState(services, "mt5")} />
    <StatusPill label="DB" state={health.data?.database ?? "UNKNOWN"} />
    <StatusPill label="TELEGRAM" state={workerState(services, "telegram")} />
    <StatusPill label="SHADOW" state={workerState(services, "shadow_worker")} />
    <StatusPill label="FORWARD" state={workerState(services, "forward_shadow_worker")} />
    <StatusPill label="NEWS" state="PLANNED" />
  </div>;
}

function BackendStatusBanner() {
  const health = useSystemHealth();
  const backend = backendState(health.data, health.error);
  if (backend === "CONNECTED") return null;
  return <div className={`backend-banner ${backend.toLowerCase()}`} role="status">
    <strong>{stateLabel(backend)}</strong>
    <span>{backend === "OFFLINE" ? "Dashboard ออนไลน์ แต่ Trading Runtime บนเครื่องไม่ได้เชื่อมต่อ" : backend === "STALE" ? "API ตอบสนองช้ากว่าปกติ — ค่าที่แสดงอาจเป็นข้อมูลเก่า" : "กำลังรอข้อมูลสุขภาพจาก read-only API"}</span>
    {health.data?.checked_at && <time dateTime={health.data.checked_at}>ตรวจล่าสุด {thaiDateTime(health.data.checked_at)}</time>}
  </div>;
}
