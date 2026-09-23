# Remote dashboard deployment

The Vercel deployment contains the React observability frontend only. The
Windows trading runtime remains responsible for MT5, Telegram Control, live
workers, Forward Shadow, SQLite, and all broker connectivity.

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

## SPA routing

The app uses browser history routes. Configure the Vercel project with
`frontend` as its root directory and a rewrite from unknown paths to
`/index.html` if the hosting project does not supply the standard Vite SPA
fallback. No runtime process or SQLite database is deployed to Vercel.
