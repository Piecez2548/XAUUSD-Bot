# Remote dashboard deployment

The Vercel deployment contains the React observability frontend only. The
Windows trading runtime remains responsible for MT5, Telegram Control, live
workers, Forward Shadow, SQLite, and all broker connectivity.

Phase 2.6 intentionally maintains two deployment surfaces:

- **Public static dashboard:** Vercel-hosted UI with no private live-data
  connection when `VITE_API_BASE_URL` is unset.
- **Private live dashboard:** a separate build served by the Windows FastAPI
  process and reached only through tailnet-only Tailscale Serve.

## Local

From `frontend/` run `npm run dev`. Vite proxies `/api` and `/ws` to the local
FastAPI process at `127.0.0.1:8000`. The frontend is read-only and always shows
`REAL-MONEY EXECUTION: DISABLED`.

## Vercel

Set the public environment variable `VITE_API_BASE_URL` to an explicitly
reachable, read-only FastAPI base URL. Set `VITE_WS_URL` only when the API's
WebSocket endpoint is separately reachable. Vite variables are public: never
place Telegram, MT5, database, or API credentials in them.

If `VITE_API_BASE_URL` is not configured in production, the dashboard remains
online but truthfully reports:

> Dashboard ออนไลน์ แต่ Trading Runtime บนเครื่องไม่ได้เชื่อมต่อ

It never falls back to localhost in a production build and never infers old
data as connected. API timestamps drive CONNECTED/STALE/OFFLINE/UNKNOWN state.

## Private live dashboard (Tailscale Serve)

The private dashboard keeps FastAPI bound to `127.0.0.1:8000`. Tailscale Serve
provides the HTTPS endpoint only inside the authorized tailnet; it is not
Tailscale Funnel and it does not expose a public port. Tailscale Serve identity
headers are required by the API when `REMOTE_DASHBOARD_MODE=true`, and the
Phase 2.6 route allowlist still denies lifecycle, write, broker, unknown, and
WebSocket routes.

Build the private same-origin variant locally on the Windows host:

```powershell
$env:VITE_PRIVATE_DASHBOARD="true"
$env:VITE_API_BASE_URL=""
npm run build
```

Start the existing read-only API with `REMOTE_DASHBOARD_MODE=true`, then an
authorized operator may configure Tailscale Serve to proxy only to
`http://127.0.0.1:8000`. The private dashboard is opened at the device's
Tailscale HTTPS/MagicDNS hostname from an enrolled, authorized phone or laptop.
Do not use `tailscale funnel` for this dashboard.

The public Vercel build must leave `VITE_PRIVATE_DASHBOARD=false` and
`VITE_API_BASE_URL` unset. No authentication secret belongs in either build.

## SPA routing

The app uses browser history routes. Configure the Vercel project with
`frontend` as its root directory and a rewrite from unknown paths to
`/index.html` if the hosting project does not supply the standard Vite SPA
fallback. No runtime process or SQLite database is deployed to Vercel.
