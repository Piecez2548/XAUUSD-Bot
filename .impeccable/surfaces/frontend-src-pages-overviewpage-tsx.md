---
version: 1
slug: "frontend-src-pages-overviewpage-tsx"
primary_target: "frontend/src/pages/OverviewPage.tsx"
related_targets: ["frontend/src/components/AppShell.tsx","frontend/src/styles.css"]
---

# Overview surface brief

## Direction

Use approved Option #2 as the primary audit-first mission-control composition. Preserve the compact dark terminal, left navigation, top status rail, Account State, XAUUSD Market State, central Audit Timeline, System Health, Risk Budget, Recent Trades, and Open Positions. Add the large Equity / Balance / Drawdown visualization from Option #1 and the realtime Event Stream from Option #3 without displacing the audit hierarchy.

## Functional contract

- Phase 1.5 is an observability terminal and remains strictly read-only.
- Never render trade-entry, close, modify, or execution controls.
- Every displayed runtime value comes from the API, database, WebSocket, or an explicit unavailable/planned state.
- MT5 and Database are CONNECTED only when the backend has verified them.
- Telegram reports CONNECTED, DISABLED, ERROR, or UNKNOWN from real state.
- AI Engine and News are PLANNED. Trade Execution is DISABLED.
- Risk copy must distinguish the 2% per-trade maximum from the 6% aggregate open-risk maximum.
- Equity filters are 1D, 1W, 1M, 3M, ALL. Empty history gets a professional empty state.
- Event Stream consumes the existing system event API/WebSocket and exposes severity/source filtering and auto-scroll.

## Visual contract

- Desktop-first at 1920x1080; also support 2560x1440, 1440x900, and 1366x768. Tablet/mobile may stack and collapse navigation.
- Palette: canvas #0B1117, raised panel #151A22, border #262C36, primary text #E7ECF3, muted text #8D99A8, restrained green #3BC784, amber #E8B04A, red #EF6262, info blue #64A7F2.
- Dense 8px spacing system, 10-12px radii, one-pixel borders, no decorative gradients or glow.
- Sans-serif UI typography with monospace for numbers, timestamps, tickets, IDs, and status telemetry.
- Compact cards, precise alignment, high contrast, visible focus, and reduced-motion-safe transitions.

## Data and empty states

- Use an em dash for absent scalar values, `Unavailable` when a capability cannot provide a value, `Unknown` when current state is not verified, and `Planned` only for documented future systems.
- Never synthesize account, market, P&L, performance, risk, AI decision, trade, or health records.
- Future fields may appear only as clearly marked placeholders on future-ready detail pages.

## Approved comp

`.impeccable/mocks/overview-option-b.png` is the approved visual reference. Options A and C are retained as provenance only.

## Approved-comp inventory

| Visible ingredient | Commitment | Shipping medium |
| --- | --- | --- |
| Left navigation | Compact scalable rail with 10 destinations and clear active state | Semantic nav + Lucide icons + CSS |
| Top status rail | Read-only mode context, five service states, UTC clock | Semantic header + live API state |
| Account / market stack | Dense paired labels and broker-derived values | React components + API data |
| Central Audit Timeline | Dominant first-viewport chronology with restrained connector line | Ordered list + CSS |
| System Health | Explicit semantic state per service; no inferred success | API-backed status pills |
| Risk Budget | 2% per-trade and 6% aggregate limits kept visually distinct | CSS progress bar + risk API |
| Equity / Drawdown | Large secondary monitoring area with 1D/1W/1M/3M/ALL | Recharts using real account snapshots |
| Event Stream | Dense UTC/severity/source/event/description/correlation grid | Existing event API + WebSocket |
| Open Positions / Recent Trades | Compact real-data tables and honest empty states | Semantic tables + API data |
| Approved mock image | Reference only; never shipped in the product bundle | Accepted omission from production |

Component grammar is flat and infrastructural: compact headers, 10px corners, one-pixel graphite seams, no shadows, and no decorative elevation. The type ramp is 8–9px telemetry/kickers, 10–12px body and panel titles, 16–20px values, and 23–29px page titles. Numbers, timestamps, tickets, and identifiers use monospace.
