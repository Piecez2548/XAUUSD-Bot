# Phase 1.5 Architecture

## System shape

```text
MetaTrader 5 (read-only calls)
        │
        ▼
services.collector → immutable MarketSnapshot
        │                         │
        │                         └→ services.risk → RiskSnapshot
        ▼
persistence.SnapshotRepository (single SQL transaction)
        │
        ▼
typed DomainEvent → EventBus → critical EventRepository
                            └→ optional TelegramNotifier → system_health

SQLite / future PostgreSQL
        │
        ├→ FastAPI GET/CSV routes
        ├→ database-backed WebSocket poller
        ├→ React Trading Observatory (served from `frontend/dist` in production)
        └→ Telegram Control Service → Process Supervisor → API + Live Engine
```

The existing `mt5` package remains the broker boundary. Higher layers consume validated domain models rather than the MT5 module. `services.collector` is the only Phase 1.5 adapter that composes connection and read services.

## Packages

| Package | Responsibility |
| --- | --- |
| `config` | Environment parsing, safe defaults, cross-field validation |
| `mt5` | Read-only connection, account, symbol, candle, tick, and position access |
| `models` | Immutable Pydantic market and observatory contracts |
| `services` | Collection orchestration, risk calculation, observation, and backup |
| `domain` | Typed, discriminated, versioned event contracts |
| `events` | Ordered in-process event delivery and subscriber isolation |
| `persistence` | SQLAlchemy engine, ORM schema, repositories, and Alembic runner |
| `analytics` | Pure calculations over closed-trade samples |
| `notifications` | Event-driven Telegram formatting and delivery |
| `api` | Read-only FastAPI routes, CSV exports, health, and WebSocket fan-out |
| `frontend` | React/TypeScript/Tailwind/Recharts observability terminal |
| `services.control` | Authenticated fixed-command Telegram control plane and polling |
| `services.supervisor` | Singleton process registry, lifecycle, and bounded crash recovery |

## Observation transaction and event order

1. Publish `SYSTEM_STARTED` to the critical database subscriber first.
2. Collect a complete validated MT5 market snapshot.
3. Calculate risk from the same immutable snapshot.
4. Persist account, symbol, closed candles, market, positions, and risk in one transaction.
5. Publish connection/snapshot/risk/completion events in order.
6. The database subscriber persists each event idempotently before optional Telegram delivery is attempted.
7. Telegram delivery outcomes append a sanitized `system_health` record; the health API and WebSocket read this same durable record.

If MT5 collection fails, `MT5_DISCONNECTED` and `SYSTEM_ERROR` are recorded, then the error is re-raised. Optional Telegram failure does not roll back durable state.

## Runtime modes

- `phase1`: unchanged human-readable live diagnostic; no database required.
- `observe`: one read-only MT5 observation, persistence, events, and optional notifications.
- `server`: migrations followed by FastAPI/WebSocket service.
- `migrate`: schema upgrade only.
- `backup`: online-safe SQLite backup through the SQLite backup API.
- `telegram-test`: explicit outbound connectivity check only.
- `control`: persistent Telegram polling and infrastructure control; execution remains disabled.

## Frontend data flow

The frontend reads only `/api/*` endpoints and `/ws/live`. The WebSocket poller uses durable `system_events` and `system_health` records, so reconnecting clients do not require a second logging or health architecture. Telegram WebSocket updates contain only component, status, timestamp, and latency; they never contain Telegram credentials. All pages display `—`, `Unavailable`, `Unknown`, `Disabled`, or `Planned` rather than manufacturing data.

## Future extension boundaries

AI, news, strategy, execution, and position-management services must publish typed events and persist version identifiers. They must not import React components or embed notification delivery. Any future execution component must remain isolated from this read-only application and requires a later explicitly approved phase.

## Phase 1.7 control boundary

The control service is a separate long-lived process. It authorizes only private
messages matching both configured chat and user allowlists, persists update
offsets, and dispatches a fixed command map. The supervisor owns only the API
and Live Data Engine children, uses an atomic local lock and sanitized registry,
and allows at most three restarts in ten minutes by default. `/stop` terminates
monitoring children without issuing any MT5 position operation.

## Phase 1.7.1 coherent live state

The live account cycle persists account, verified open positions, market state,
and derived risk through one `SnapshotRepository.persist` transaction. A
verified empty `positions_get()` result closes current position rows; a failed
read leaves the last verified state unchanged. Risk and position consumers
compare the shared market snapshot ID and expose `LIVE`, `STALE`, or
`STATE_SYNC_PENDING` freshness.
# Phase 1.6 Live Data Engine

`python main.py live` runs one `MT5Gateway` boundary shared by tick, account,
candle, history, and watchdog workers. The gateway serializes all synchronous
MetaTrader 5 calls through one asyncio lock. Workers publish the existing typed
domain events and persist sanitized health records; no order or execution API is
reachable from this runtime.
