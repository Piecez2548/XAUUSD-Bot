# Security and Read-only Controls

## Enforced Phase 1.5 boundary

- No execution endpoint exists.
- No BUY, SELL, CLOSE, MODIFY, or order-ticket UI exists.
- MT5 modules expose connection and reads only.
- `mt5.order_send` is absent from production code.
- The dashboard permanently displays `READ ONLY` and `ORDER EXECUTION DISABLED`.
- Future trade/decision tables and event types are inert audit contracts.

## Secrets

- `.env` is git-ignored.
- MT5 passwords and Telegram tokens are `repr=False` and included in log redaction.
- `/api/config/public` exposes only non-secret policy values.
- API/CSV/WebSocket responses exclude MT5 credentials, Telegram tokens/chat IDs, and prompt content. Telegram health stores and broadcasts only a bounded outcome/status, timestamp, latency, and fixed safe message.
- Partial MT5 credential configuration fails closed.

Use OS-level secret storage or deployment secret injection outside local development. Never commit `.env`, databases, backups, logs, or broker exports.

## Network surface

The default API host is loopback only. CORS accepts configured frontend origins and GET-oriented headers/methods. Remote access requires TLS, authentication, authorization, rate limits, CSRF/origin controls, secure secret storage, and network isolation before changing the bind address.

## Data integrity

- Pydantic models reject unexpected fields, non-finite values, naive timestamps, invalid ranges, and payload/event mismatches.
- Repository writes use transactions with rollback.
- SQLite foreign keys and WAL are enabled.
- Domain events are idempotent by UUID and versioned.
- Closed candles are unique by symbol/timeframe/timestamp.
- Optional notification failure is isolated from critical persistence.

## Logging and future execution

Phase 1 logs use structured JSONL and redaction. System errors must never include credentials or tokens. Public health routes return bounded states, not exception traces.

Any future execution work requires a separate threat model, explicit authorization, broker permission checks, risk gates, idempotent order intent, reconciliation, kill switch, and independent audit—not a hidden extension of the observatory.
# Phase 1.7 control-plane safeguards

Telegram control is disabled and deny-by-default unless explicitly enabled.
Every command must match both an allowed chat ID and allowed user ID. Updates
are processed by a fixed command map with persisted offsets; no Telegram text is
passed to a shell, `eval`, `exec`, or arbitrary subprocess command. State-changing
commands share a serialized operation lock and rate limiter. Audit records keep
only safe actor identifiers, command, authorization result, result, timestamp,
and correlation ID. Process termination is conservative: an unverified reused
PID is never killed.
