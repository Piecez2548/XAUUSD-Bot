# Phase 2.6 — Secure Remote Read-Only Data Acceptance

Date: 2026-09-23 (Asia/Bangkok)
Repository: `D:\Project_001\Nexus-Project\XAUUSD Bot`
Starting SHA: `872742a1cb8077c0beaaf20ac9e2d9dfdf1b465e`
Ending SHA: `aafb7594035ca0ec7afe60edc58b88cbe5e4e3c0` (Phase 2.6 changes pushed in two logical commits)

## Status

**PHASE 2.6: PRIVATE TAILNET LIVE DASHBOARD ACCEPTED**

The operator provisioned and authorized Tailscale Serve. Local verification
reported Tailscale online with MagicDNS and the tailnet-only HTTPS endpoint:

```text
https://piecez2548.tail708f84.ts.net/
  -> http://127.0.0.1:8000
```

`tailscale serve status --json` contains only a tailnet HTTPS handler. Funnel
was not enabled. The private route boundary, frontend transport behavior,
route inventories, and security regression tests are complete. The controlled
API-only production acceptance ran with `REMOTE_DASHBOARD_MODE=true`; MT5,
Telegram, Live Engine, Forward Shadow, supervisor, registry, Pair Zone, and
broker state were not modified.

The production Vercel deployment remains a public static frontend and is not
reported as connected to live Windows data. The private live dashboard is
served only through the tailnet endpoint.

## Baseline and protected scope

- `main` and `origin/main` were aligned at `872742a1cb8077c0beaaf20ac9e2d9dfdf1b465e` before this work.
- Existing untracked Phase 2.5 reports and local artifacts were not staged,
  deleted, or rewritten.
- `127.0.0.1:8000` remains the FastAPI bind address.
- `services/supervisor.py`, `services/control.py`, and `mt5/bootstrap.py` were
  not modified.
- No `/start`, `/stop`, `/restart`, MT5, Telegram, broker, or live runtime
  action was invoked. The authorized API-only process was started, stopped,
  and restarted for gateway acceptance. Supervisor implementation/configuration
  was untouched; the existing supervisor API start updated the registry
  truthfully and no manual registry edit was performed.
- Pair Zone V1, Forward Shadow history/session, and execution-disabled state
  were not changed.

## Gateway architecture evaluation

### Previously preferred, now superseded: Cloudflare Tunnel + Cloudflare Access

The intended architecture is an outbound Windows `cloudflared` connector to a
Cloudflare HTTPS hostname, with Cloudflare Access identity authentication and a
strict edge route allowlist forwarding only to `http://127.0.0.1:8000`. This
keeps the API loopback-bound, avoids router port forwarding, provides TLS and
identity enforcement, and is reversible by removing the tunnel route.

Cloudflare's published application model supports mapping a public hostname to
a local service, while its Access CORS guidance explicitly requires deliberate
handling of authentication cookies and preflight requests:

- [Cloudflare Tunnel setup](https://developers.cloudflare.com/tunnel/get-started/)
- [Cloudflare Access CORS behavior](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/)

The operator rejected purchasing or registering a custom domain. The public
Cloudflare architecture is therefore not used for this phase. Tailscale is
installed and authenticated on this host; no Cloudflare tunnel or public API
route was created.

### No-custom-domain evaluation

Cloudflare's current Workers platform provides a technically viable
development/pilot path without a custom domain:

```text
Vercel browser
    |
HTTPS + Cloudflare Access
    |
protected <worker>.<account>.workers.dev
    |
strict Worker BFF allowlist
    |
Workers VPC Service binding
    |
outbound cloudflared Tunnel from Windows
    |
127.0.0.1:8000
```

The Worker cannot fetch the Windows `127.0.0.1` address directly. It must use
Workers VPC with a VPC Service or VPC Network binding, and the Windows host
must run an outbound Cloudflare Tunnel connector. The Worker itself can be the
browser-facing BFF: exact `GET` paths only, no arbitrary upstream URL, exact
Vercel-origin CORS, no write methods, and no secret in `VITE_*`. Access can
protect a `workers.dev` hostname.

This no-domain path is **not selected for the production acceptance** because
Cloudflare currently documents Workers VPC as beta and explicitly warns that
its APIs may change before general availability. It also adds a new Worker
runtime, VPC binding, and BFF deployment surface to a release whose security
boundary must remain small and auditable. `workers.dev` is described as
appropriate for personal/hobby use, while Cloudflare recommends a custom
domain or route for production Workers.

The alternatives were evaluated as follows:

| Option | No custom domain | Private Windows origin | Identity | Decision |
|---|---:|---:|---:|---|
| Direct Cloudflare Tunnel + Access published app | No | Yes | Access | Preferred production target; requires a Cloudflare-managed domain |
| `workers.dev` + Access + Workers VPC + Tunnel | Yes | Yes | Access | Technically viable pilot; not accepted as production because VPC is beta |
| Tailscale Serve | Yes | Yes | Tailnet identity | Selected for the private live dashboard |
| Tailscale Funnel | Yes | Yes | None by default | Unsafe for this private API without another identity gateway |
| TryCloudflare Quick Tunnel | Yes | Yes | Not a production identity boundary | Explicitly rejected; development-only |

The operator-approved production scope is now a **private live dashboard for
authorized tailnet devices**. The public Vercel site remains static-only. No
custom domain is required or will be purchased for this scope.

Reference documentation: [Workers `workers.dev`](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/), [Workers VPC](https://developers.cloudflare.com/workers-vpc/), and [Cloudflare Tunnel routing](https://developers.cloudflare.com/tunnel/concepts/routing/).

## Operator-approved architecture: Private Live Dashboard

```text
authorized phone/laptop
        |
Tailscale user/device identity + encrypted tailnet transport
        |
Tailscale Serve HTTPS/MagicDNS hostname (tailnet-only)
        |
FastAPI serving private frontend + API
        |
REMOTE_DASHBOARD_MODE strict route boundary
        |
127.0.0.1:8000
```

The existing FastAPI application already serves `frontend/dist` and the
read-only API from one loopback origin. A separate private build uses relative
`/api/*` paths, so the browser does not need CORS, a public API URL, or a
permanent credential in JavaScript. Tailscale Serve provides encrypted
tailnet-only HTTPS and forwards trusted `Tailscale-User-Login` identity
headers. The API requires that header in `REMOTE_DASHBOARD_MODE` and applies
the Phase 2.6 exact GET allowlist before dispatching any API request.

Tailscale Funnel is explicitly excluded. It would make the endpoint public and
would not provide the private tailnet boundary required by this architecture.

The public/private split is intentional:

- **PUBLIC STATIC DASHBOARD:** `https://xauusd-bot-mu.vercel.app/`; no private
  live data and no `VITE_API_BASE_URL`.
- **PRIVATE LIVE DASHBOARD:** Tailscale Serve hostname; authorized enrolled
  phone/laptop only, same-origin live data, no Vercel dependency.

### Browser authentication design

The superseded public Cloudflare design required browser Access cookies and
cross-origin CORS. The approved private design avoids that browser problem:
Tailscale authenticates the device/user before Serve forwards the request, and
the browser uses the same HTTPS origin for frontend and API. The private
dashboard must:

1. require the selected operator identity before returning private data;
2. forward only an allowlisted `GET` path to `127.0.0.1:8000`;
3. fail closed for missing Tailscale identity, unknown paths, and every
   non-GET API method; and
4. keep `/ws/live` denied until a separate WSS acceptance is completed.

The frontend now treats WSS as opt-in. `VITE_WS_URL` remains unset, so Phase
2.6 uses HTTPS polling and does not repeatedly infer an unprovisioned WSS
endpoint.

## Dashboard data inventory

The machine-readable frontend consumer inventory is:

`PHASE_2_6_REMOTE_DATA_INVENTORY_2026-09-23.json`

It records consumer, method, exact path, purpose, polling frequency, required
status, and sensitivity. The current minimum HTTP allowlist is:

```text
GET /api/account
GET /api/config/public
GET /api/decisions
GET /api/export/decisions.csv
GET /api/export/trades.csv
GET /api/forward/health
GET /api/forward/performance
GET /api/forward/session
GET /api/forward/trades
GET /api/live/status
GET /api/positions
GET /api/positions/status
GET /api/performance/account-curve
GET /api/performance/by-confidence
GET /api/performance/by-direction
GET /api/performance/by-hour
GET /api/performance/by-session
GET /api/performance/by-weekday
GET /api/performance/cumulative-r
GET /api/performance/monthly
GET /api/performance/pnl-by-day
GET /api/performance/summary
GET /api/research/compare
GET /api/research/datasets
GET /api/research/robustness
GET /api/research/runs
GET /api/research/strategies
GET /api/risk/current
GET /api/shadow/decision
GET /api/shadow/decisions
GET /api/shadow/health
GET /api/shadow/outcome-health
GET /api/shadow/outcomes
GET /api/shadow/performance
GET /api/shadow/summary
GET /api/supervisor/status
GET /api/symbol
GET /api/system/events
GET /api/system/health
GET /api/trades
GET /api/trades/{trade_id}
GET /api/trades/{trade_id}/events
GET /api/research/runs/{run_id}/curve
```

Parameter paths accept only one non-slash identifier. Query strings may carry
the dashboard's bounded filters, but never select an upstream URL or change
the route decision. `/ws/live` is intentionally outside this Phase 2.6
allowlist.

The complete FastAPI route inventory, including denied routes and the
Telegram-only lifecycle surface, is:

`PHASE_2_6_FASTAPI_ROUTE_INVENTORY_2026-09-23.json`

## Remote denylist classes

The edge must deny, independently of frontend behavior:

- Telegram lifecycle commands: `/start`, `/stop`, `/restart`;
- POST, PUT, PATCH, DELETE, and other non-GET methods;
- broker writes, order placement, position modification, and execution;
- process control, supervisor mutation, configuration mutation, and secrets;
- unknown FastAPI routes and `/ws/live` until separately secured;
- arbitrary proxy/upstream URL parameters;
- encoded path separators, encoded dot segments, traversal, duplicate
  slashes, absolute URLs, fragments, and malformed targets;
- unauthenticated or expired sessions.

## Regression coverage completed locally

`config/remote_read_only_policy.py` is a fail-closed contract for the future
edge adapter. It is not a public proxy and does not open a network listener.

`tests/test_phase26_remote_boundary.py` covers:

- allowed dashboard observability routes;
- lifecycle denial;
- POST, PUT, PATCH, DELETE, and HEAD denial;
- unknown and non-minimum route denial;
- traversal, encoded path, duplicate-slash, absolute-URL, and backslash
  bypass denial;
- query-string and arbitrary upstream URL denial;
- broker-write path denial;
- authentication failure denial;
- exact-origin CORS preflight handling without upstream data.

`tests/test_phase26_private_dashboard.py` covers:

- required Tailscale identity header;
- authenticated allowlisted GET success;
- unknown-route and lifecycle-surface denial;
- POST, PUT, PATCH, and DELETE denial;
- WSS denial while polling remains the approved transport.

Validation completed:

```text
46 passed, 2 warnings
pytest tests/test_phase26_remote_boundary.py tests/test_phase26_private_dashboard.py tests/test_phase254_remote_security.py -q --tb=short
frontend: 20 tests passed, lint PASS, production build PASS
git diff --check: PASS
both Phase 2.6 JSON inventories: parse successfully
```

The 46 backend tests and 20 frontend tests are local contract/security tests.
Controlled production Serve validation completed on 2026-09-23:

```text
Tailscale status: online; MagicDNS enabled; tailnet IPv4 100.106.163.56
Serve: HTTPS tailnet-only -> 127.0.0.1:8000
Serve root through HTTPS: 200
Serve /api/system/health through HTTPS: 200
Serve /start: 404
Serve POST /api/system/health: 404
Production API stopped: Serve returned 502; local port 8000 had no listener
Production API restored: Serve returned 200
```

The initial gateway checks used an isolated fixture; the controlled production
window then repeated the positive, negative, and failure/recovery checks with
the approved API process and persisted `REMOTE_DASHBOARD_MODE=true`. The
production health response reported `database=CONNECTED`, `mt5=CONNECTED`,
`trade_execution=DISABLED`, and `error_count=0`. The recorded live runtime,
Telegram, Forward Shadow, Pair Zone, and session evidence remained unchanged;
no lifecycle or trading action was invoked. After the final supervised API
start, the API registry state was `RUNNING`/desired `RUNNING` with
`restart_count=4`; the preserved Live record remained `STOPPED` with
`restart_count=2`.

## Vercel configuration

- Production URL remains `https://xauusd-bot-mu.vercel.app/`.
- `VITE_API_BASE_URL` remains unset because the public Vercel deployment is
  intentionally static-only under the operator-approved architecture.
- A separate private Windows build uses `VITE_PRIVATE_DASHBOARD=true` and an
  empty `VITE_API_BASE_URL`, producing same-origin relative API requests.
- `VITE_WS_URL` remains unset by design; HTTPS polling is the Phase 2.6
  transport target and WSS is optional future work.
- No authentication secret is in any `VITE_*` variable or frontend bundle.
- No backend, trading runtime, or MT5 process is deployed to Vercel.

## Acceptance matrix

| Requirement | Status | Evidence / limitation |
|---|---|---|
| Public static Vercel frontend | PASS from Phase 2.5 | Existing production deployment; intentionally no live private data |
| Private live dashboard architecture | PASS | Tailscale Serve + loopback FastAPI production path exercised |
| Tailscale authentication | PASS on host | Tailscale online, MagicDNS enabled, Serve identity header observed through tailnet HTTPS |
| Private HTTPS transport | PASS | `piecez2548.tail708f84.ts.net` is tailnet-only and proxies only loopback 8000 |
| Remote read-only data | PASS | All 40 concrete frontend GET paths returned 200 through production Serve |
| Route allowlist | PASS locally and through production Serve | Policy, middleware tests, and HTTPS lifecycle/write denial checks passed |
| API supervisor ownership | PASS | Existing supervisor API start recorded verified API PID/tree truthfully; no manual registry edit |
| Control endpoint isolation | PASS by design | Telegram control is outside FastAPI and denied by policy |
| Broker-write isolation | PASS locally | Existing API route audit plus boundary tests; no `mt5.order_send` production path exposed |
| HTTPS | PASS | Tailscale Serve HTTPS responded successfully; no public endpoint |
| CORS | NOT REQUIRED for private live dashboard | Frontend and API share the Tailscale Serve origin |
| Failure/recovery | PASS | API-only stop produced 502 with no listener; API-only recovery returned 200 |
| Secret audit | PASS for this change | No credentials or secret values added; value contents not printed |
| Real-money execution | DISABLED | API-only window; no broker writes or execution capability exposed |
| Forward Shadow | PRESERVED | Runtime/history/session untouched |
| Pair Zone V1 | PRESERVED | Strategy configuration untouched |
| Git baseline pushed | PASS | `HEAD == origin/main` after selective Phase 2.6 commits |

## What remains before Phase 2 can close

The acceptance requirements and Git closeout are complete. `HEAD` matches
`origin/main` after selective Phase 2.6 commits. The original public Vercel
live-data criterion was intentionally replaced and is not claimed as passed.

## Rollback procedure

Rollback is limited to the approved API-only surface: stop the API process,
return `REMOTE_DASHBOARD_MODE=false` if the private dashboard is intentionally
retired, and remove the Tailscale Serve handler only through the reversible
Serve procedure. The public Vercel build remains static-only. Never expose port
8000 or change the Windows API bind address during rollback.

## Final conclusion

Phase 2.6 acceptance is complete for the operator-approved architecture: a
tailnet-only private live dashboard plus a public static Vercel dashboard.
Tailscale Serve provisioning, production HTTPS delivery, strict route denial,
secret isolation, execution-disabled state, and API-only failure/recovery are
proven. The original public Vercel live-data acceptance criterion was
intentionally replaced and is not claimed as passed. Phase 2 is closed and
the repository is ready for Phase 3.
