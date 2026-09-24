# Phase 3.5.4 — CSRF and Private Mutation Security

## Mutation inventory

The FastAPI route table is guarded by a regression test that requires an
explicit policy classification for every registered POST, PUT, PATCH, or
DELETE operation. The current inventory is:

| Classification | Routes |
| --- | --- |
| `PRE_AUTH_EXCEPTION` | `POST /api/auth/login`; `POST /api/auth/enroll` |
| `CSRF_REQUIRED` | `POST /api/auth/logout`; `POST /api/auth/admin/accounts`; `POST /api/auth/admin/accounts/{account_id}/disable`; `DELETE /api/auth/admin/accounts/{account_id}/sessions`; `DELETE /api/auth/admin/accounts/{account_id}/sessions/{session_id}` |
| `CSRF_REQUIRED` | `POST /api/control/start`; `POST /api/control/stop`; `POST /api/control/restart`; `POST /api/control/demo-on`; `POST /api/control/demo-off` |

All other registered API operations are GET/read-only. Unknown unsafe methods
and unclassified mutation paths fail closed before reaching an endpoint.

## Token and session binding

Authenticated clients obtain a token from `GET /api/auth/csrf`. The route
requires an active application session and the matching Tailscale identity,
and returns `Cache-Control: no-store`. Each token contains a fresh 256-bit
CSPRNG nonce and an HMAC-SHA256 authenticator keyed by that session's opaque
session cookie. The server stores no CSRF token or CSRF secret; comparison is
constant-time. A token from another session cannot validate, and expired,
revoked, or disabled sessions fail authentication before token validation.
Session revocation therefore invalidates all tokens for that session without
a schema migration.

The browser API helper keeps the token in module memory only, fetches it after
authentication, and adds `X-CSRF-Token` to private mutations. It does not place
the token in a URL, local storage, or session storage, and does not retry a
rejected mutation. GET requests do not acquire or send mutation authority.

## Request provenance and pre-authentication exceptions

Set `CSRF_TRUSTED_ORIGINS` to the exact comma-separated origin(s) serving the
private dashboard, including scheme and non-default port where applicable,
with no paths or wildcards. An unset/empty list denies every mutation. Missing,
malformed, or non-matching `Origin` is denied; `Host` is not used as evidence.
No Referer fallback is used. If `Sec-Fetch-Site` is present, only `same-origin`,
`same-site`, or `none` is accepted; missing metadata is tolerated because the
exact Origin and token checks remain mandatory for authenticated mutations.

Login and enrollment secret consumption are the only pre-authentication
exceptions to session-bound CSRF: no valid application session exists yet.
Both still require Tailscale identity and exact trusted Origin; cross-site
Fetch Metadata is rejected. Login retains its password and identity checks.
Enrollment additionally requires its specific unexpired, unused secret and
the bound Tailscale identity. No general pre-authentication bypass exists.

## Enforcement and observability

The private-dashboard middleware applies route classification, Origin and
Fetch Metadata checks, session/identity verification, and CSRF validation
before endpoint code. This is before named-pipe lifecycle IPC, Demo-control
dispatch, logout revocation, or OWNER mutation. Errors are generic:
`AUTH_REQUIRED`, `CSRF_REJECTED`, or `FORBIDDEN`; expected token values are
never returned. CSRF rejections are not written to audit records or logs and
do not generate Telegram notifications.

The route policy does not alter lifecycle operation IDs, Control/named-pipe
authority, Demo safety gates, or trading behavior. No database migration is
required. `CSRF_TRUSTED_ORIGINS` must be configured before browser mutations
will succeed; leaving it unset is intentionally fail-closed.

CSRF protection is not deployment authorization. Production migration,
OWNER bootstrap, and deployment remain separate operator-controlled steps.
