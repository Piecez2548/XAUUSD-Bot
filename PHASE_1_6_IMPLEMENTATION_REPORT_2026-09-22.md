# Phase 1.6 Implementation Report — 2026-09-22

## 1. Executive summary

Implemented the read-only Live Data Engine around the existing MT5, event, health, persistence, API, WebSocket, Telegram, and dashboard architecture. No Phase 2 capability or trade execution was added.

## 2. Regression

The pre-existing Phase 1.5 behavior remains covered; the full Python suite passes with the new Phase 1.6 tests.

## 3. Files

Changed configuration, domain events, MT5 adapters, live services, persistence, API, CLI, frontend types/views, documentation, migration, and tests. New core modules are `services/live.py`, `services/reconstruction.py`, `models/live.py`, `models/history.py`, `mt5/gateway.py`, and `mt5/history.py`.

## 4. Migrations

Migration `20260922_0002_live_engine` adds `candle_cursors`, `broker_deals`, and `history_cursors`. A clean temporary SQLite migration was applied successfully.

## 5. Runtime

`python main.py live` starts the continuous runtime and keeps the original `phase1` default and one-shot `observe` command intact.

## 6. Workers and cadence

Tick, account/positions, candle, history, and watchdog workers use independent configuration-driven cadences (defaults 1s, 5s, 5s, 30s, and watchdog heartbeat).

## 7. Serialization

All MT5 calls pass through one `MT5Gateway` asyncio lock and `asyncio.to_thread`; workers cannot concurrently call the synchronous MT5 module.

## 8. Reconnect

Connection failures transition to `DISCONNECTED`/`RECONNECTING` and retry with bounded exponential backoff. Initial synchronization also enters the reconnect loop after a transient failure.

## 9. Freshness

Tick, account, positions, and history timestamps are exposed as `LIVE`, `STALE`, or `UNKNOWN`; symbol trade-mode text can distinguish `MARKET_CLOSED` for stale ticks.

## 10. Tick

The fast worker reads broker ticks and updates in-memory freshness without fabricating prices or emitting synthetic ticks.

## 11. Account

Account snapshots are persisted at the account cadence and only emit `ACCOUNT_UPDATED` when verified account values change.

## 12. Positions

Open positions are synchronized by broker ticket. New/changed/no-longer-open observations are explicit; disappearance never infers an exit reason.

## 13. History

`history_deals_get` is read-only, UTC-normalized, symbol-scoped, overlap-windowed, and persisted by unique deal ticket with a cursor.

## 14. Trade reconstruction

`services/reconstruction.py` reconstructs only positions with both broker entry and exit facts sharing a position id. Missing exits, stops, direction, and reasons remain unavailable.

## 15. Candle engine

M5, M15, H1, and H4 workers query position 1 onward, excluding the forming candle, persist unique closed bars, and emit typed `CANDLE_CLOSED` events.

## 16. Missed-candle recovery

A per-symbol/timeframe cursor causes all unseen completed bars in the lookback window to be replayed chronologically after downtime.

## 17. Risk

Risk is recalculated from broker specifications and observed positions at account cadence and persisted through the existing risk repository.

## 18. Unbounded risk

Missing stop-loss values produce truthful unbounded risk and `POSITION_WITHOUT_STOP_LOSS`/`RISK_UNBOUNDED` transitions; no risk number is fabricated.

## 19. Telegram live and dedup

Live events reuse the existing Telegram subscriber and sanitized health persistence. New event titles/templates are covered; no credentials enter events, health payloads, API, WebSocket, frontend, or logs.

## 20. WebSocket

The existing `/ws/live` polling hub streams persisted domain events and sanitized health updates. Live workers publish through the same EventBus; no parallel health architecture was introduced.

## 21. Dashboard

The dashboard now reads `/api/live/status` and shows runtime plus per-stream freshness alongside existing truthful service health.

## 22. Retention and indexing

Deal and candle tables have uniqueness and time/position/symbol indexes. Existing append-only event/health retention behavior is preserved.

## 23. Crash and restart

Cursors, unique deal tickets, unique candles, WAL SQLite configuration, and idempotent repositories make restart/replay safe. Runtime shutdown cancels workers and closes MT5.

## 24. Tests

Added freshness-state, zero-throttle configuration, history cursor/idempotence, and conservative reconstruction tests. Existing Telegram disabled/unknown/connected/degraded/error/recovery tests remain active.

## 25. Exact results

Python: `58 passed, 2 warnings`. Ruff: `All checks passed`. Python compileall: passed. Frontend: `4 tests passed`; TypeScript, ESLint, and Vite production build passed.

## 26. Frontend validation

`npm run test -- --run`, `npm run typecheck`, `npm run lint`, and `npm run build` all completed successfully.

## 27. Live MT5 tests

No live broker credentials were available for an automated test run in this environment. The live path is covered with deterministic adapters/unit contracts and remains read-only.

## 28. Safety

No `mt5.order_send`, order modification, close, execution endpoint, AI decision loop, news service, or Phase 2 implementation was added.

## 29. Limitations

Market-closed classification relies on broker-provided symbol trade-mode text; provider-specific session calendars are intentionally not invented. Live broker smoke testing still requires the operator’s MT5 terminal.

## 30. Manual tests

Run `python main.py migrate`, then `python main.py live` with a logged-in MT5 terminal. Confirm `/api/live/status`, `/api/system/health`, and `/ws/live`; stop with Ctrl+C and restart to verify cursor/idempotence behavior.

## 31. Commands

`python main.py observe` remains one-shot. `python main.py live` runs continuously. `python main.py server` serves the API/dashboard. `python main.py telegram-test` records Telegram health after a successful request.

## 32. Next step

Operator-side MT5 smoke validation and deployment-specific retention policy review are the next safe steps. Phase 2 remains explicitly out of scope.

## 33. Data-integrity notes

All persisted timestamps are UTC-aware at the application boundary. Provider payloads are mapped to typed fields; raw deal payload storage is disabled (`raw_payload=None`) to avoid accidental secret propagation.

## 34. Completion status

Phase 1.6 implementation is complete within the requested read-only scope. No trade execution or autonomous decision capability is enabled.
