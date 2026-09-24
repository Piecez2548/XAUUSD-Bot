"""Stateless, session-bound CSRF tokens for private browser mutations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

_TOKEN_BYTES = 64
_NONCE_BYTES = 32
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{86}$")
_TOKEN_CONTEXT = b"xauusd-private-csrf-v1\x00"


def issue_csrf_token(session_token: str) -> str:
    """Create a 256-bit-random nonce authenticated with this session cookie."""

    if not isinstance(session_token, str) or not session_token:
        raise ValueError("authenticated session token is required")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    mac = hmac.new(
        session_token.encode("utf-8"), _TOKEN_CONTEXT + nonce, hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(nonce + mac).rstrip(b"=").decode("ascii")


def validate_csrf_token(session_token: str | None, supplied: str | None) -> bool:
    """Verify token shape and session MAC with constant-time comparison."""

    if (
        not isinstance(session_token, str)
        or not session_token
        or not isinstance(supplied, str)
        or not _TOKEN_PATTERN.fullmatch(supplied)
    ):
        return False
    try:
        decoded = base64.urlsafe_b64decode(supplied + "==")
    except (ValueError, binascii.Error):
        return False
    if len(decoded) != _TOKEN_BYTES:
        return False
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(canonical, supplied):
        return False
    nonce, supplied_mac = decoded[:_NONCE_BYTES], decoded[_NONCE_BYTES:]
    expected_mac = hmac.new(
        session_token.encode("utf-8"), _TOKEN_CONTEXT + nonce, hashlib.sha256
    ).digest()
    return hmac.compare_digest(expected_mac, supplied_mac)
