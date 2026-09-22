# Phase 2.5.2 — Telegram Thai UX Report

## Final status

**PHASE 2.5.2 IMPLEMENTED — MANUAL THAI TELEGRAM UX VALIDATION REQUIRED**

This phase changes Telegram presentation only. Canonical health values,
database timestamps, strategy rules, risk calculations, research calculations,
Forward Shadow state, and execution policy are unchanged.

## Files changed

- `notifications/telegram_th.py` — centralized Thai presentation helpers for
  health/trade state labels, worker names, timezone formatting, numeric values,
  execution banner, status rows, and `/help`.
- `services/control.py` — routes existing Telegram responses through the
  presentation helpers; adds the existing authoritative `worker:forward_shadow`
  state to `/health`; keeps canonical technical identifiers alongside Thai
  explanations where useful for audit compatibility.
- `tests/test_phase252_telegram_thai.py` — regression coverage for state
  mapping, command coverage, Forward health/session/timezone presentation,
  empty Forward trades, secret omission, and execution-disabled messaging.

The working tree also contains the previously uncommitted Phase 2.5.1 files
(`mt5/bootstrap.py`, `.env.example`, `config/settings.py`, its report and
tests). They were not committed, pushed, tagged, or otherwise altered by this
phase beyond the existing control integration.

No frontend files were changed. Nexus was not modified.

## Translation architecture

`notifications.telegram_th` is a presentation-only layer. It receives already
authoritative values and does not query MT5, calculate health, change strategy
results, or write database records. `services.control.TelegramControlService`
remains the command dispatcher and uses the existing repositories/services for
all data.

Canonical identifiers remain visible where operationally useful, for example
`🟢 ปกติ (CONNECTED)`, `NO_TRADE`, `BUY`, `SELL`, `TP`, `SL`, `RR`, `R`, run
IDs, session IDs, hashes, and strategy versions.

## Status mapping

CONNECTED/HEALTHY → `🟢 ปกติ`  
RUNNING/ACTIVE → `🟢 กำลังทำงาน`  
DEGRADED/PAUSED/INTERRUPTED → `🟡` operator warning  
UNKNOWN → `⚪ ยังไม่ทราบสถานะ`  
ERROR/FAILED → `🔴` error/failure  
DISABLED/STOPPED → `⚫` disabled/stopped

`/health` now presents the same authoritative Forward Shadow worker source
(`worker:forward_shadow`) used by the existing health architecture. No second
health calculation was introduced.

## Timezone presentation

Persisted UTC timestamps are untouched. Telegram display uses
`Asia/Bangkok (UTC+7)`, for example `22 ก.ย. 2026 22:43 น.`, with an explicit
UTC audit line where useful.

## Command coverage

Thai operator descriptions are provided for every currently registered command
in `/help`: control, market/account, Shadow, research/backtest, Forward
Validation, dashboard, logs, and help. `/start`, `/stop`, and `/restart` keep
their existing lifecycle behavior and now provide Thai progress/results. Empty
Forward states explicitly explain that no signal is not an error. Research and
performance responses remain evidence-only and do not claim live suitability.

## Validation results

- `pytest -q`: **175 passed, 2 warnings** (the existing httpx/Starlette
  deprecation warnings only).
- `ruff check .`: **passed**.
- `python -m compileall -q .`: **passed**.
- `git diff --check`: **passed**.
- Frontend Vitest/TypeScript/ESLint/build: **not run; no frontend files were
  touched**.

## Safety and non-regression

- `pair_zone_v1` config SHA-256 remains
  `fd2d73b9aa0d21004653e455263107caf727ac552fd204486f363cb7c25b7ded`.
- No `mt5.order_send` production source path was found.
- Execution remains `DISABLED`; no broker writes were added.
- No Forward session reset or database migration for translation was performed.
- `.env`, database files, process registry, Telegram offset, frontend
  dependencies/build output, runtime logs, backups, and credential values were
  not added to the tracked change set. `logs/.gitkeep` is only the existing
  directory marker.
- No Telegram token, chat ID, MT5 password, API key, or database credential is
  emitted by the presentation layer.
- No process was terminated or restarted during implementation.

## Manual acceptance procedure

1. Leave the existing Telegram Control process running; do not restart the
   live/API/control processes.
2. Send `/help`, `/health`, `/status`, `/forwardhealth`, `/forward`,
   `/forwardtrades`, `/forwardperformance`, `/risk`, `/decision`, `/start`,
   `/stop`, and `/restart` from the authorized private chat.
3. Confirm commands remain English and responses are primarily Thai, with
   technical identifiers preserved.
4. Confirm `/health` and `/forwardhealth` show the same Forward Shadow state,
   and that Bangkok display time is UTC+7 while UTC audit time remains correct.
5. Confirm the active Forward session ID is unchanged before and after
   `/stop` → `/start` and `/restart`.
6. Confirm no token, chat ID, password, API key, stack trace, or other secret
   appears in any Telegram response or `/logs` output.
7. Confirm every response states that real-money trading is disabled and that
   no broker order is sent.

Do not mark this phase fully accepted until this live Telegram procedure has
been observed by the operator.
