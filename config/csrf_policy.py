"""Exact route and request-provenance policy for private cookie mutations."""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from urllib.parse import urlsplit


class MutationClass(StrEnum):
    CSRF_REQUIRED = "CSRF_REQUIRED"
    PRE_AUTH_EXCEPTION = "PRE_AUTH_EXCEPTION"
    READ_ONLY = "READ_ONLY"


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PRE_AUTH_MUTATIONS = frozenset(
    {
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/enroll"),
    }
)
CSRF_REQUIRED_MUTATIONS = frozenset(
    {
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/admin/accounts"),
        ("POST", "/api/control/start"),
        ("POST", "/api/control/stop"),
        ("POST", "/api/control/restart"),
        ("POST", "/api/control/demo-on"),
        ("POST", "/api/control/demo-off"),
    }
)
_ACCOUNT_DISABLE = re.compile(r"^/api/auth/admin/accounts/[0-9a-fA-F-]{36}/disable$")
_ACCOUNT_SESSIONS = re.compile(r"^/api/auth/admin/accounts/[0-9a-fA-F-]{36}/sessions$")
_ACCOUNT_SESSION = re.compile(
    r"^/api/auth/admin/accounts/[0-9a-fA-F-]{36}/sessions/[0-9a-fA-F-]{36}$"
)


def classify_mutation(method: str, path: str) -> MutationClass | None:
    """Classify an HTTP operation; unknown unsafe routes fail closed."""

    verb = method.upper()
    if verb in SAFE_METHODS:
        return MutationClass.READ_ONLY
    if (verb, path) in PRE_AUTH_MUTATIONS:
        return MutationClass.PRE_AUTH_EXCEPTION
    if (verb, path) in CSRF_REQUIRED_MUTATIONS:
        return MutationClass.CSRF_REQUIRED
    if verb == "POST" and (
        path == "/api/auth/admin/accounts/{account_id}/disable"
        or _ACCOUNT_DISABLE.fullmatch(path)
    ):
        return MutationClass.CSRF_REQUIRED
    if verb == "DELETE" and path in {
        "/api/auth/admin/accounts/{account_id}/sessions",
        "/api/auth/admin/accounts/{account_id}/sessions/{session_id}",
    }:
        return MutationClass.CSRF_REQUIRED
    if verb == "DELETE" and (_ACCOUNT_SESSIONS.fullmatch(path) or _ACCOUNT_SESSION.fullmatch(path)):
        return MutationClass.CSRF_REQUIRED
    return None


def origin_key(value: str) -> tuple[str, str, int] | None:
    """Return a canonical HTTP origin tuple, rejecting URL-shaped extras."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(char.isspace() or ord(char) < 0x20 for char in value) or "?" in value or "#" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.netloc.endswith(":")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "%" in parsed.netloc
        ):
            return None
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host or "*" in host:
        return None
    try:
        canonical_host = ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            canonical_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if not canonical_host or canonical_host.startswith(".") or canonical_host.endswith("."):
            return None
    scheme = parsed.scheme.lower()
    resolved_port = port if port is not None else (443 if scheme == "https" else 80)
    if not 1 <= resolved_port <= 65535:
        return None
    return scheme, canonical_host, resolved_port


def is_trusted_origin(origin: str | None, trusted_origins: tuple[str, ...]) -> bool:
    """Match Origin against configured origins by exact canonical tuple."""

    supplied = origin_key(origin) if origin is not None else None
    if supplied is None:
        return False
    return any(origin_key(candidate) == supplied for candidate in trusted_origins)


def fetch_metadata_is_acceptable(value: str | None) -> bool:
    """Reject cross-site or malformed Fetch Metadata when the browser sends it."""

    if value is None:
        return True
    return value.strip().lower() in {"same-origin", "same-site", "none"}
