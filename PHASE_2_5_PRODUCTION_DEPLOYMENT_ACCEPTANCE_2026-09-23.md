# XAUUSD AI TRADER — PHASE 2.5 PRODUCTION DEPLOYMENT ACCEPTANCE

Date: 2026-09-23 (Asia/Bangkok)

## Final decision

The static observability frontend is deployed and accepted on Vercel. The
deployment is static-only: no FastAPI runtime, SQLite database, MT5, Telegram
Control, supervisor, broker connection, or secret was deployed to Vercel.

Remote authenticated live data is **not deployed**. The Windows API remains a
private/local service and no public gateway or WSS endpoint was created.

## 1. Starting baseline and ending commit

Starting baseline:

```text
branch: main
HEAD/origin/main: 91b3fccaf13d291f2962f3848e1e864edf7d25dc
```

Deployment-fix commit:

```text
f6135e041a93fdef945990ed53caca13ae2cbd6c
fix(frontend): support direct forward validation preview route
```

The fix was selectively committed and pushed to `origin/main`. No unrelated
historical artifact was staged.

## 2. Vercel project configuration

| Setting | Verified value |
|---|---|
| Provider | Vercel |
| Team | `piecez2548's projects` / Hobby |
| Project | `xauusd-bot` |
| Repository | `Piecez2548/XAUUSD-Bot` |
| Branch | `main` |
| Root directory | `frontend` |
| Framework | Vite |
| Build | Vite production build (`npm run build`) |
| Output | `dist` |
| SPA fallback | `frontend/vercel.json` rewrites unknown paths to `/index.html` |

Vercel initially detected 48 environment-variable names from repository
example/configuration files. All 48 were removed from the deployment form
before deployment. No Vercel environment variable was configured.

## 3. Deployment URLs and actual deployment history

The first import was automatically assigned to the Vercel Production
environment by the Vercel UI; a separate Preview deployment was not created.
This is recorded explicitly rather than being mislabeled as Preview.

Initial deployment from the accepted baseline (superseded):

- Deployment URL: `https://xauusd-6w2j1nmz0-piecez2548s-projects.vercel.app/`
- Commit: `91b3fcc...`
- Result: static app loaded, but `/forward-validation` exposed a 404 because
  the source route only had `/forward`.

Current fixed deployment:

- Deployment URL: `https://xauusd-dfqzotpii-piecez2548s-projects.vercel.app/`
- Production domain: `https://xauusd-bot-mu.vercel.app/`
- Deployment record: `CZ1QMN4TmScFvUYphyP4dTXpZhPY`
- Commit: `f6135e041a93fdef945990ed53caca13ae2cbd6c`
- Vercel status: Ready

## 4. Static route acceptance

Verified against the fixed deployment URL:

| Route | Direct navigation | Refresh | Result |
|---|---:|---:|---|
| `/` | Pass | Pass | Overview renders |
| `/research` | Pass | Pass | Strategy Lab renders |
| `/forward-validation` | Pass | Pass | Forward Validation renders |

The production domain was also directly checked at
`/forward-validation` and remained healthy after refresh. The route fix adds
`/forward-validation` as an alias to the existing read-only
`ForwardValidationPage`; the existing `/forward` route remains unchanged.

The static dashboard truthfully displayed:

- `OFFLINE` / `UNKNOWN` because no API endpoint was configured;
- no localhost fallback;
- blank/unavailable runtime values rather than inferred old data;
- `REAL-MONEY EXECUTION: DISABLED` and `ORDER EXECUTION DISABLED`.

## 5. Remote gateway architecture

Current architecture is static-only Option E:

```text
Browser --HTTPS--> Vercel static frontend
                     (no remote API configured)
Windows host: private FastAPI 127.0.0.1:8000
```

No authenticated edge/gateway, tunnel, VPN, relay, public hostname, or
WSS endpoint was created. The smallest acceptable future remote architecture
remains an authenticated HTTPS gateway with a strict read-only route allowlist
in front of the private Windows API. Direct exposure of port 8000 is not
allowed.

## 6. Authentication, route allowlist, and isolation

Remote authentication status: **NOT DEPLOYED**.

The repository has no bearer/OIDC/session authentication layer for remote API
access. CORS is an origin policy, not authentication. Therefore no remote live
API route is claimed as externally accepted.

For a future gateway, default-deny is required. Only explicitly required
read-only GET observability routes may be allowed. The following must remain
blocked externally:

- `/start`, `/stop`, `/restart`, Telegram/lifecycle equivalents;
- all `POST`, `PUT`, `PATCH`, and `DELETE` routes;
- broker/order/position mutation and any `mt5.order_send` capability;
- secret/configuration retrieval and generic API passthrough;
- unprotected CSV exports unless separately authorized;
- wildcard WebSocket exposure.

Static frontend isolation result: **PASS**. The deployed frontend contains no
lifecycle or broker-write capability and no configured remote API.

## 7. CORS, HTTPS, and WSS

- Vercel traffic is HTTPS.
- No insecure public `http://` or `ws://` API endpoint was configured.
- `VITE_WS_URL` is unset; no WSS claim is made.
- FastAPI CORS remains an explicit allowlist with credentials disabled and GET
  methods only; it was not changed by this deployment.
- Production remote live-data CORS is not accepted because the gateway/API
  endpoint does not exist yet.

## 8. Environment variables by name

Vercel environment variable set: **empty**.

The frontend supports these public names, both unset:

```text
VITE_API_BASE_URL
VITE_WS_URL
```

No Telegram, MT5, database, broker, gateway, bearer, or API secret was placed
in Vercel or a `VITE_*` variable.

## 9. Validation and security evidence

Frontend validation after the route fix:

```text
npm --prefix frontend test       PASS — 10 files, 20 tests
npm --prefix frontend run lint   PASS — zero findings
npm --prefix frontend run build  PASS — Vite/TypeScript build
```

Safe backend/security validation:

```text
.venv\Scripts\python.exe -m pytest \
  tests/test_phase254_remote_security.py \
  tests/test_phase255_performance.py -q --tb=short
```

Result: **7 passed**, with two existing dependency deprecation warnings.

Additional audits:

- no secret-like or insecure remote endpoint hit was found in `frontend/dist`;
- no production `mt5.order_send(` implementation was found in production
  source;
- no protected supervisor/MT5/lifecycle working-tree diff;
- no Pair Zone V1 or Forward Shadow history/session mutation;
- no Telegram lifecycle command, MT5, broker, or live runtime action was
  invoked by this deployment work.

## 10. Production runtime preservation

- Real-money execution: **DISABLED**.
- Telegram remains the lifecycle/control plane.
- MT5 state was not changed.
- Forward Shadow state/history/session was not changed.
- SQLite and the Windows process registry were not deployed or modified.
- No Vercel function or backend trading runtime was created.

## 11. Files changed and commits

Deployment-fix commit:

```text
frontend/src/App.tsx
frontend/src/routes.tsx
frontend/src/App.test.tsx
```

This report is a new documentation file and is intentionally not yet included
in the prior deployment-fix commit. It is to be committed separately after
this report is reviewed.

Commits created during this task:

```text
f6135e0 fix(frontend): support direct forward validation preview route
```

Push status: pushed successfully to `origin/main`; no force push was used.

## 12. Remaining limitations

- No separately deployed Preview environment was created because Vercel's
  import flow created the initial deployment as Production.
- Remote authenticated live data is not deployed.
- No gateway provider, identity policy, public API hostname, exact production
  remote CORS origin, or WSS endpoint exists yet.
- Vercel CLI was not available locally; the authenticated Vercel browser
  session was used for project import and verification.
- The production domain is a Vercel-generated domain; no custom DNS/domain
  decision was made.

## 13. Rollback procedure

No rollback was executed. The Vercel project exposes the prior deployment and
an Instant Rollback control. If the route-fix deployment must be reverted,
operator review should select the previous known-ready deployment in Vercel or
revert commit `f6135e0` with a normal reviewed Git revert. Do not force-push,
delete deployments, or roll back runtime/MT5 state as part of frontend
rollback.

## 14. Final status

| Required status | Result |
|---|---|
| STATIC FRONTEND | **PASS** |
| REMOTE AUTH | **NOT DEPLOYED** |
| REMOTE READ-ONLY DATA | **NOT DEPLOYED** |
| CONTROL ENDPOINT ISOLATION | **PASS** for the static frontend; gateway not deployed |
| BROKER WRITE ISOLATION | **PASS** |
| PRODUCTION DEPLOYMENT | **PASS** — fixed static deployment Ready |
| GIT | **SELECTIVELY CLEAN** — tracked deployment changes committed/pushed; historical untracked artifacts preserved |

Phase 2.5 static frontend deployment can be closed. The remote live-data
phase must remain open until an authenticated read-only gateway and its
 end-to-end security acceptance are separately provisioned and verified.
