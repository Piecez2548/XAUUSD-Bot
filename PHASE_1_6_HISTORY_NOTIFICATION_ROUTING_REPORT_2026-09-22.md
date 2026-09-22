# Phase 1.6 History Notification Routing Fix — 2026-09-22

## 1. Root cause

Every successful history polling cycle published `HISTORY_SYNC_COMPLETED`, and Telegram subscribed to every domain event. The 30-second reconciliation cadence therefore became a repeated operator notification.

## 2. Telegram routing policy

Telegram now uses an explicit operator-alert allowlist. Event persistence and WebSocket observability remain unchanged; routing is filtered only in the Telegram subscriber.

## 3. Telemetry-only events

`HISTORY_SYNC_COMPLETED`, `ACCOUNT_UPDATED`, and `CANDLE_CLOSED` are telemetry-only. Routine snapshot/heartbeat-style events remain silent. History freshness, cursor, worker health, and persisted deal facts continue updating.

## 4. Telegram-notifiable events

System lifecycle, MT5 connect/disconnect/recovery, meaningful risk transitions, position-open/lifecycle events, stale-data alerts, critical errors, and history health transitions (`HISTORY_SYNC_FAILED` / `HISTORY_SYNC_RECOVERED`) remain notifiable. New deals do not emit a generic history alert.

## 5. Files changed

Changed `notifications/telegram.py`, `services/live.py`, `domain/events.py`, and `notifications/templates.py`. Added regression coverage in `tests/test_phase16_live.py`.

## 6. Tests added

Tests cover silent repeated history sync, continued freshness, no-change Telegram silence, persisted/idempotent history behavior, failure transition once, repeated failure suppression, recovery once, and routing classification.

## 7. Exact regression results

Python: `60 passed, 2 warnings`. Ruff: `All checks passed`. Compileall: passed. Existing frontend checks remain green: 4 tests passed, TypeScript passed, ESLint passed, and production build passed.

## 8. Manual verification required

Run `python main.py live` for at least two minutes with no trading activity. Telegram should not receive a message every 30 seconds, while dashboard history freshness and worker health continue updating. Execution remains disabled and no Phase 2 work was started.
