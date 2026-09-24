"""Application authentication core for the tailnet-private dashboard."""

from __future__ import annotations

import hashlib
import logging
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select

from config.settings import Settings
from persistence.database import Database
from persistence.orm import AuthAuditRecord, AuthSessionRecord, AuthUserRecord, new_id

SESSION_COOKIE_NAME = "__Host-xauusd_session"
ACTIVE_ROLES = frozenset({"OWNER", "ADMIN"})
ACCOUNT_STATES = frozenset(
    {"PROVISIONED", "PENDING_APPROVAL", "ACTIVE", "LOCKED", "REVOKED"}
)
_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
_LOG = logging.getLogger(__name__)


def normalize_login(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: str
    login: str
    role: str
    state: str
    session_id: str
    idle_expires_at: datetime
    absolute_expires_at: datetime

    def safe_payload(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "user": {"id": self.id, "login": self.login, "role": self.role, "state": self.state},
            "session": {
                "idle_expires_at": self.idle_expires_at.isoformat(),
                "absolute_expires_at": self.absolute_expires_at.isoformat(),
            },
        }


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: AuthenticatedUser | None
    token: str | None


class AuthenticationService:
    """Small DB-backed auth service; no provisioning or approval endpoints."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    @staticmethod
    def hash_password(password: str) -> str:
        if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
            raise ValueError("Password must contain 12 to 1024 characters")
        return _PASSWORD_HASHER.hash(password)

    @staticmethod
    def verify_password(password_hash: str, password: str) -> bool:
        try:
            return _PASSWORD_HASHER.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
            return False

    def create_user_for_admin(
        self,
        *,
        login: str,
        password: str,
        role: str,
        state: str,
        bound_tailscale_login: str,
    ) -> str:
        """Internal provisioning primitive; deliberately not exposed over HTTP."""

        normalized = normalize_login(login)
        tailscale_login = normalize_login(bound_tailscale_login)
        if not normalized or len(normalized) > 254 or not tailscale_login:
            raise ValueError("Login and bound Tailscale identity are required")
        if role not in ACTIVE_ROLES or state not in ACCOUNT_STATES:
            raise ValueError("Unsupported account role or state")
        user_id = new_id()
        with self.database.session() as session:
            session.add(
                AuthUserRecord(
                    id=user_id,
                    normalized_login=normalized,
                    password_hash=self.hash_password(password),
                    role=role,
                    state=state,
                    bound_tailscale_login=tailscale_login,
                )
            )
        return user_id

    def login(self, login: str, password: str, tailscale_login: str) -> LoginResult:
        normalized = normalize_login(login)[:254]
        identity = normalize_login(tailscale_login)[:254]
        try:
            with self.database.session() as session:
                user = session.scalar(
                    select(AuthUserRecord).where(AuthUserRecord.normalized_login == normalized)
                )
                password_hash = user.password_hash if user else _DUMMY_PASSWORD_HASH
                password_ok = self.verify_password(password_hash, password)
                allowed = bool(
                    user
                    and password_ok
                    and user.state == "ACTIVE"
                    and user.role in ACTIVE_ROLES
                    and identity
                    and user.bound_tailscale_login == identity
                )
                if not allowed:
                    session.add(
                        AuthAuditRecord(
                            event_type="LOGIN_FAILURE",
                            user_id=user.id if user else None,
                            normalized_login=normalized or None,
                            tailscale_login=identity or None,
                            outcome="DENIED",
                            reason="INVALID_CREDENTIALS",
                        )
                    )
                    return LoginResult(None, None)

                now = datetime.now(UTC)
                absolute_expiry = now + timedelta(hours=self.settings.auth_session_absolute_hours)
                idle_expiry = min(
                    now + timedelta(minutes=self.settings.auth_session_idle_minutes),
                    absolute_expiry,
                )
                token = secrets.token_urlsafe(32)
                session_id = new_id()
                session.add(
                    AuthSessionRecord(
                        id=session_id,
                        user_id=user.id,
                        token_hash=_token_hash(token),
                        created_at=now,
                        last_seen_at=now,
                        idle_expires_at=idle_expiry,
                        absolute_expires_at=absolute_expiry,
                    )
                )
                session.add(
                    AuthAuditRecord(
                        event_type="LOGIN_SUCCESS",
                        user_id=user.id,
                        normalized_login=user.normalized_login,
                        tailscale_login=identity,
                        session_id=session_id,
                        outcome="SUCCEEDED",
                    )
                )
                authenticated = AuthenticatedUser(
                    user.id,
                    user.normalized_login,
                    user.role,
                    user.state,
                    session_id,
                    idle_expiry,
                    absolute_expiry,
                )
            return LoginResult(authenticated, token)
        except Exception:
            _LOG.warning("Authentication login lookup failed closed")
            raise

    def authenticate(self, token: str | None, tailscale_login: str) -> AuthenticatedUser | None:
        if not token or len(token) > 256:
            return None
        identity = normalize_login(tailscale_login)
        now = datetime.now(UTC)
        try:
            with self.database.session() as session:
                auth_session = session.scalar(
                    select(AuthSessionRecord).where(
                        AuthSessionRecord.token_hash == _token_hash(token)
                    )
                )
                if auth_session is None or auth_session.revoked_at is not None:
                    return None
                user = session.get(AuthUserRecord, auth_session.user_id)
                if (
                    auth_session.absolute_expires_at <= now
                    or auth_session.idle_expires_at <= now
                ):
                    auth_session.revoked_at = now
                    session.add(
                        AuthAuditRecord(
                            event_type="SESSION_EXPIRED",
                            user_id=auth_session.user_id,
                            tailscale_login=identity or None,
                            session_id=auth_session.id,
                            outcome="DENIED",
                            reason=(
                                "ABSOLUTE_TIMEOUT"
                                if auth_session.absolute_expires_at <= now
                                else "IDLE_TIMEOUT"
                            ),
                        )
                    )
                    return None
                if (
                    user is None
                    or user.state != "ACTIVE"
                    or user.role not in ACTIVE_ROLES
                    or not identity
                    or user.bound_tailscale_login != identity
                ):
                    return None

                touch_interval = timedelta(seconds=self.settings.auth_session_touch_seconds)
                if now - auth_session.last_seen_at >= touch_interval:
                    auth_session.last_seen_at = now
                    auth_session.idle_expires_at = min(
                        now + timedelta(minutes=self.settings.auth_session_idle_minutes),
                        auth_session.absolute_expires_at,
                    )
                return AuthenticatedUser(
                    user.id,
                    user.normalized_login,
                    user.role,
                    user.state,
                    auth_session.id,
                    auth_session.idle_expires_at,
                    auth_session.absolute_expires_at,
                )
        except Exception:
            _LOG.warning("Authentication session lookup failed closed")
            raise

    def revoke(self, token: str | None, tailscale_login: str) -> None:
        if not token or len(token) > 256:
            return
        identity = normalize_login(tailscale_login)
        now = datetime.now(UTC)
        try:
            with self.database.session() as session:
                auth_session = session.scalar(
                    select(AuthSessionRecord).where(
                        AuthSessionRecord.token_hash == _token_hash(token)
                    )
                )
                if auth_session is None or auth_session.revoked_at is not None:
                    return
                user = session.get(AuthUserRecord, auth_session.user_id)
                if user is None or user.bound_tailscale_login != identity:
                    return
                auth_session.revoked_at = now
                session.add(
                    AuthAuditRecord(
                        event_type="SESSION_REVOKED",
                        user_id=auth_session.user_id,
                        tailscale_login=identity or None,
                        session_id=auth_session.id,
                        outcome="SUCCEEDED",
                        reason="LOGOUT",
                    )
                )
                session.add(
                    AuthAuditRecord(
                        event_type="LOGOUT",
                        user_id=auth_session.user_id,
                        tailscale_login=identity or None,
                        session_id=auth_session.id,
                        outcome="SUCCEEDED",
                    )
                )
        except Exception:
            _LOG.warning("Authentication logout failed closed")
            raise
