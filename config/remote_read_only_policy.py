"""Fail-closed contract for the future authenticated remote data gateway.

This module is intentionally a policy contract, not an Internet-facing proxy.
The selected gateway must enforce the same decisions before forwarding to the
loopback FastAPI listener.  Keeping the contract in repository code makes the
route boundary testable without starting MT5, Telegram, or a real gateway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

REMOTE_FRONTEND_ORIGIN = "https://xauusd-bot-mu.vercel.app"
REMOTE_UPSTREAM_HOST = "127.0.0.1"
REMOTE_UPSTREAM_PORT = 8000

# These are the only HTTP methods the edge may handle.  OPTIONS is a gateway
# CORS preflight response and must never be forwarded to FastAPI.
REMOTE_HTTP_METHODS = frozenset({"GET", "OPTIONS"})

# These paths are private operator controls, not part of the public/read-only
# gateway contract.  FastAPI may expose them only after its Tailscale identity
# middleware has authenticated the request.
PRIVATE_CONTROL_GET_PATHS = frozenset({
    "/api/control/status",
    "/api/control/demo-status",
})
PRIVATE_CONTROL_POST_PATHS = frozenset({
    "/api/control/start",
    "/api/control/stop",
    "/api/control/restart",
    "/api/control/demo-on",
    "/api/control/demo-off",
})
AUTH_ROUTE_METHODS = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/session"),
}

# Exact paths are intentionally used instead of a prefix such as /api.  The
# two path families below cover only identifiers used by the dashboard.
REMOTE_ALLOWED_EXACT_PATHS = frozenset(
    {
        "/api/account",
        "/api/config/public",
        "/api/decisions",
        "/api/export/decisions.csv",
        "/api/export/trades.csv",
        "/api/forward/health",
        "/api/forward/performance",
        "/api/forward/session",
        "/api/forward/trades",
        "/api/intelligence/alerts",
        "/api/intelligence/candidate",
        "/api/intelligence/context",
        "/api/intelligence/evidence",
        "/api/live/status",
        "/api/positions",
        "/api/positions/status",
        "/api/performance/account-curve",
        "/api/performance/by-confidence",
        "/api/performance/by-direction",
        "/api/performance/by-hour",
        "/api/performance/by-session",
        "/api/performance/by-weekday",
        "/api/performance/cumulative-r",
        "/api/performance/monthly",
        "/api/performance/pnl-by-day",
        "/api/performance/summary",
        "/api/research/compare",
        "/api/research/datasets",
        "/api/research/robustness",
        "/api/research/runs",
        "/api/research/strategies",
        "/api/risk/current",
        "/api/shadow/decision",
        "/api/shadow/decisions",
        "/api/shadow/health",
        "/api/shadow/outcome-health",
        "/api/shadow/outcomes",
        "/api/shadow/performance",
        "/api/shadow/summary",
        "/api/supervisor/status",
        "/api/symbol",
        "/api/system/events",
        "/api/system/health",
        "/api/trades",
    }
)

_REMOTE_ALLOWED_PARAMETER_PATHS = (
    re.compile(r"^/api/research/runs/[^/]+/curve$"),
    re.compile(r"^/api/trades/[^/]+$"),
    re.compile(r"^/api/trades/[^/]+/events$"),
)


@dataclass(frozen=True)
class RemoteBoundaryDecision:
    """The deterministic result a gateway adapter must enforce."""

    allowed: bool
    reason: str
    path: str | None = None


def _safe_path(raw_target: str) -> str | None:
    """Return a canonical path, rejecting ambiguous URL spellings.

    Query strings are deliberately ignored after parsing, but encoded path
    separators, encoded dots, backslashes, NULs, absolute URLs, fragments,
    duplicate slashes, and dot segments fail closed instead of being decoded
    into a different route.
    """

    if not raw_target or not raw_target.startswith("/") or raw_target.startswith("//"):
        return None
    try:
        target = urlsplit(raw_target)
    except ValueError:
        return None
    if target.scheme or target.netloc or target.fragment:
        return None
    path = target.path
    if not path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    # Do not accept any percent-encoded path spelling.  This prevents encoded
    # slash/dot bypasses and keeps the edge's route comparison unambiguous.
    if "%" in path or unquote(path) != path:
        return None
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments[1:]):
        return None
    return path


def is_remote_path_allowed(raw_target: str) -> bool:
    """Return whether a target path is in the dashboard's exact allowlist."""

    path = _safe_path(raw_target)
    if path is None:
        return False
    return path in REMOTE_ALLOWED_EXACT_PATHS or any(
        pattern.fullmatch(path) for pattern in _REMOTE_ALLOWED_PARAMETER_PATHS
    )


def is_private_control_path_allowed(raw_target: str, method: str) -> bool:
    """Allow only the exact private Web control path/method combinations."""

    path = _safe_path(raw_target)
    normalized_method = method.upper()
    if normalized_method == "GET":
        return path in PRIVATE_CONTROL_GET_PATHS
    if normalized_method == "POST":
        return path in PRIVATE_CONTROL_POST_PATHS
    return False


def is_auth_route_allowed(raw_target: str, method: str) -> bool:
    """Allow only the three exact Tailscale-gated auth-core routes."""

    path = _safe_path(raw_target)
    return (method.upper(), path) in AUTH_ROUTE_METHODS


def authorize_remote_request(
    *,
    method: str,
    raw_target: str,
    authenticated: bool,
    origin: str | None = None,
) -> RemoteBoundaryDecision:
    """Evaluate one request before any upstream connection is made.

    ``authenticated`` represents a provider-verified identity/session result;
    it is not a browser-supplied header and is not implemented here.  OPTIONS
    is handled at the edge for the exact Vercel origin and never forwarded.
    """

    normalized_method = method.upper()
    path = _safe_path(raw_target)
    if normalized_method not in REMOTE_HTTP_METHODS:
        return RemoteBoundaryDecision(False, "method_not_allowed", path)
    if path is None or not is_remote_path_allowed(raw_target):
        return RemoteBoundaryDecision(False, "path_not_allowlisted", path)
    if normalized_method == "OPTIONS":
        if origin != REMOTE_FRONTEND_ORIGIN:
            return RemoteBoundaryDecision(False, "origin_not_allowed", path)
        return RemoteBoundaryDecision(True, "cors_preflight_no_upstream", path)
    if not authenticated:
        return RemoteBoundaryDecision(False, "authentication_required", path)
    return RemoteBoundaryDecision(True, "authenticated_read_only_route", path)


__all__ = [
    "REMOTE_ALLOWED_EXACT_PATHS",
    "REMOTE_FRONTEND_ORIGIN",
    "REMOTE_HTTP_METHODS",
    "REMOTE_UPSTREAM_HOST",
    "REMOTE_UPSTREAM_PORT",
    "PRIVATE_CONTROL_GET_PATHS",
    "PRIVATE_CONTROL_POST_PATHS",
    "AUTH_ROUTE_METHODS",
    "RemoteBoundaryDecision",
    "authorize_remote_request",
    "is_private_control_path_allowed",
    "is_auth_route_allowed",
    "is_remote_path_allowed",
]
