# Operations

## Local start

```powershell
# Terminal 1
.\.venv\Scripts\Activate.ps1
python main.py migrate
python main.py server

# Terminal 2
cd frontend
npm run dev
```

The API binds to `127.0.0.1:8000` by default and the frontend to `127.0.0.1:5173`. CORS is limited to configured local origins. For a packaged deployment, serve `frontend/dist` through a trusted local web server and keep the API behind the same host or a restrictive reverse proxy.

## Phase 2.6 private live dashboard

The approved no-custom-domain architecture separates the public static Vercel
dashboard from the private live dashboard. The private dashboard is served by
the same FastAPI process that serves `frontend/dist`, but is reachable only
through tailnet-only Tailscale Serve:

```text
authorized phone/laptop
        |
Tailscale identity + encrypted tailnet transport
        |
Tailscale Serve HTTPS/MagicDNS hostname
        |
FastAPI 127.0.0.1:8000
        |
strict Phase 2.6 GET allowlist
```

`REMOTE_DASHBOARD_MODE=true` is an explicit fail-closed mode. It requires the
trusted `Tailscale-User-Login` header, denies non-GET API methods, denies
unknown API paths, denies `/ws/live` until WSS is separately secured, and
leaves `/start`, `/stop`, `/restart`, broker writes, and process mutation
outside the HTTP surface. The default is `false`, so existing local/runtime
behavior is unchanged.

For a private build, set `VITE_PRIVATE_DASHBOARD=true` and leave
`VITE_API_BASE_URL` empty so the browser uses same-origin `/api/*` paths. Keep
the public Vercel build with `VITE_PRIVATE_DASHBOARD=false` and no API base.
Install/login/device authorization for Tailscale and enabling HTTPS in the
tailnet are explicit operator actions; do not use Tailscale Funnel or expose
port 8000 directly.

## Phase 1.7 Telegram control

Build the frontend once with `npm run build`, then configure both Telegram
allowlists (`TELEGRAM_ALLOWED_CHAT_IDS` and `TELEGRAM_ALLOWED_USER_IDS`) and
explicitly enable `TELEGRAM_CONTROL_ENABLED=true`. Start only the control plane:

```powershell
python main.py control
```

The authorized commands `/start`, `/stop`, `/restart`, `/status`, `/health`,
`/account`, `/market`, `/positions`, `/risk`, `/logs`, and `/help` use fixed
handlers. `/start` launches API and live monitoring; it never enables execution.
The production dashboard is served locally at `http://127.0.0.1:8000/`.

Optional Task Scheduler scripts are explicit operator actions:
`scripts/install_telegram_control_task.ps1`,
`scripts/uninstall_telegram_control_task.ps1`, and
`scripts/status_telegram_control_task.ps1`.

## Read-only observation

```powershell
python main.py observe
```

A successful command prints three persisted IDs. The dashboard changes MT5 to `CONNECTED` only after a fresh verified connection event; an old success becomes `UNKNOWN` after five minutes. Database state is checked live.

Telegram health is durable and follows these states:

- `DISABLED`: `TELEGRAM_ENABLED=false`.
- `UNKNOWN`: enabled with a complete configuration, but no API request has succeeded or failed yet.
- `CONNECTED`: a Telegram API request or notification delivery has succeeded.
- `DEGRADED`: the latest delivery cycle failed temporarily/recoverably.
- `ERROR`: configuration is incomplete, Telegram rejected the request, or three consecutive recoverable delivery cycles failed.

## Telegram

Set `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. The token/chat ID are excluded from settings representations and the public API. Test explicitly with `python main.py telegram-test`.

Delivery uses three bounded attempts with exponential backoff. Every test or delivery result is appended to `system_health`; `/api/system/health` and `/ws/live` expose the resulting sanitized state without credentials. A successful `python main.py telegram-test` therefore persists `CONNECTED`. Telegram is an optional event subscriber: failure is logged but cannot roll back the persisted observation/event.

## Backup and restore

Run `python main.py backup`. Backups use SQLite's online backup API and write a non-overwriting UTC timestamped file to `BACKUP_DIRECTORY` (default `backups`). Backups and live databases are git-ignored.

Restore drill:

1. Stop the API and observation process.
2. Keep the current database as a separate rollback copy.
3. Copy the selected backup to a new explicit filename under `data/`.
4. Point `DATABASE_URL` to the restored filename.
5. Run `python main.py migrate` for newer additive migrations.
6. Verify `/api/health`, row counts, recent timestamps, and CSV exports.
7. Run tests before making the restored database the normal URL.

The automated test suite opens a backup as a fresh database and verifies the schema is queryable.

## Diagnostics

- `/api/health`: API/database liveness and read-only marker.
- `/api/system/health`: verified service semantics and error count.
- `/api/system/events`: durable audit records with severity/source filters.
- `/ws/live`: database-backed domain-event fan-out.
- `/api/export/*.csv`: trade, AI decision, and trade-event exports.

An empty database is valid. Account, market, positions, trades, risk, and charts show honest empty states until real data exists.

## Failure recovery

- MT5 discovery ambiguity: set the exact broker symbol in `TRADING_SYMBOL`.
- Invalid/short candle history: resolve broker availability; data is not padded.
- SQLite lock pressure: keep one observation writer; consider PostgreSQL before concurrency.
- Telegram outage: durable events remain in the database.
- Frontend cannot reach API: verify both processes and proxy/origin settings. Values remain blank rather than cached or invented.

## Phase 1.7.1 position freshness

After a manual broker close, wait for one successful Live Data Engine account/
position cycle before querying Telegram. `/positions` and `/risk` should show
the same observed timestamp and `LIVE` freshness. `STALE` means the last
verified cycle is older than the configured threshold; `STATE_SYNC_PENDING`
means the persisted position and risk snapshots do not yet share one cycle.
An MT5 `positions_get()` failure is not an empty result and never clears the
last verified current state.
# Continuous live mode

Run `python main.py live` after applying migrations. Cadences and stale thresholds
are configured with the `LIVE_*`, `MT5_RECONNECT_*`, and `DATA_STALE_*` settings in
`.env`. The process reconnects with bounded exponential backoff, records worker
health, and stops cleanly on Ctrl+C. `GET /api/live/status` exposes persisted
runtime/freshness state and `/ws/live` continues to stream the same sanitized
domain and health events.
