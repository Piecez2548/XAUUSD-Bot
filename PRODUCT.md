# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

The backend is Python 3.11+, FastAPI, SQLAlchemy, Alembic, and SQLite with a PostgreSQL-ready
database boundary. The frontend is a separate React, TypeScript, Vite, Tailwind CSS, and
Recharts package. This stack is explicitly required by the Phase 1.5 brief.

## Users

The primary user is the engineer/operator responsible for a local Windows-based autonomous
XAUUSD research and trading system. They monitor live system state, inspect persistent records,
trace decisions and position lifecycles, investigate failures, and evaluate performance before
future autonomous execution is permitted.

## Product Purpose

XAUUSD AI Trader provides a trustworthy observation and research layer over read-only
MetaTrader 5 data. Phase 1.5 makes every observed state and future decision attributable,
persistent, exportable, and inspectable without granting the application any ability to alter
the MT5 account.

Success means the operator can answer what the system observed, what event occurred, what
configuration produced it, whether supporting services are healthy, and how completed trades
performed using reproducible formulas.

## Positioning

The product is an audit-first trading observatory: normalized MT5 observations flow through a
typed event boundary into persistence, notifications, APIs, realtime views, and analytics.
Future AI and execution components must join this same event and audit architecture rather than
bypassing it.

## Operating Context

- Runs locally on Windows beside MetaTrader 5 Desktop.
- Uses UTC internally while presenting readable local or UTC times explicitly.
- Must continue recording when optional Telegram or frontend services are unavailable.
- Supports an eventual VPS/cloud deployment without rewriting domain, event, analytics, or
  persistence logic.
- Real mode starts with an empty history and never fabricates trades or decisions.

## Capabilities and Constraints

- XAUUSD/gold only in the current system.
- Phase 1.5 covers observability, persistence, notifications, analytics, API, realtime delivery,
  dashboard, export, health monitoring, and backup.
- MT5 access remains strictly read-only. There are no BUY, SELL, CLOSE, MODIFY, pending-order,
  or SL/TP mutation paths or controls.
- Secrets remain server-side and cannot enter public configuration, API responses, exports, or
  logs.
- Historical records are append-oriented and UTC-timestamped with stable unique identifiers.
- Future actions and data fields may be represented in schemas, but Phase 1.5 does not generate
  decisions, fabricate unavailable values, or enforce execution risk.
- Future deterministic limits are 2% maximum risk per trade and 6% aggregate open risk; Phase
  1.5 may calculate and display risk read-only.

## Brand Commitments

The product name is **XAUUSD AI Trader** and the Phase 1.5 surface is called the **Trading
Observatory**. The product voice is factual, precise, calm, and operational. It must never feel
like a casino, signal-selling product, or gamified trading terminal.

## Evidence on Hand

- A verified read-only Phase 1 implementation and 27-test baseline are present in this project.
- A live MT5 demo-terminal smoke test previously validated connection, XAUUSD discovery, four
  candle timeframes, snapshot creation, and clean shutdown.
- No real completed-trade or AI-decision history is available. The interface must use honest
  empty states and must not invent production statistics.
- No logo, photography, customer claims, or commercial proof assets were supplied.

## Product Principles

1. Auditability before autonomy.
2. Fail closed when market, risk, identity, or data integrity is uncertain.
3. Preserve the distinction between observed facts, derived analytics, and unavailable values.
4. Keep optional integrations isolated so core recording continues during partial failure.
5. Make operational state legible without adding execution affordances.

## Accessibility & Inclusion

The responsive web dashboard must be keyboard-operable, use semantic landmarks and tables,
retain visible focus states, avoid color-only status communication, respect reduced motion, and
maintain readable contrast in its dark-first presentation.
