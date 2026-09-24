"""Application authentication core for the tailnet-private dashboard."""

from __future__ import annotations

import hashlib
import logging
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from config.settings import Settings
from persistence.database import Database
from persistence.orm import (
    AuthAuditRecord,
    AuthEnrollmentRecord,
    AuthSessionRecord,
    AuthUserRecord,
    new_id,
)

SESSION_COOKIE_NAME = "__Host-xauusd_session"
ACTIVE_ROLES = frozenset({"OWNER", "ADMIN"})
ENROLLMENT_TTL = timedelta(minutes=15)
ENROLLMENT_SECRET_BYTES = 32


class AuthRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"


class AccountState(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


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


@dataclass(frozen=True, slots=True)
class EnrollmentIssue:
    account: dict[str, object]
    secret: str
    expires_at: datetime


class AuthenticationService:
    """DB-backed authentication and owner-governed account provisioning."""

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
        """Legacy test fixture helper; production account issuance uses invitations."""

        if not self.database.is_disposable_test_database:
            raise PermissionError("Fixture account provisioning is restricted to disposable tests")
        normalized = normalize_login(login)
        tailscale_login = normalize_login(bound_tailscale_login)
        if not normalized or len(normalized) > 254 or not tailscale_login:
            raise ValueError("Login and bound Tailscale identity are required")
        if role != AuthRole.ADMIN.value or state not in ACCOUNT_STATES:
            raise ValueError("Only test ADMIN fixtures may use this helper")
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

    @staticmethod
    def _audit(
        session,
        *,
        event_type: str,
        outcome: str,
        actor_id: str | None = None,
        target_id: str | None = None,
        login: str | None = None,
        tailscale_login: str | None = None,
        reason: str | None = None,
    ) -> None:
        session.add(
            AuthAuditRecord(
                event_type=event_type,
                actor_user_id=actor_id,
                user_id=target_id or actor_id,
                normalized_login=login,
                tailscale_login=tailscale_login,
                outcome=outcome,
                reason=reason,
            )
        )

    def bootstrap_owner(self, *, login: str, password: str, bound_tailscale_login: str) -> str:
        """Create the sole OWNER; DB uniqueness makes concurrent attempts safe."""

        normalized = normalize_login(login)
        identity = normalize_login(bound_tailscale_login)
        if not normalized or len(normalized) > 254 or not identity or len(identity) > 254:
            raise ValueError("A valid login and Tailscale identity are required")
        password_hash = self.hash_password(password)
        owner_id = new_id()
        try:
            with self.database.session() as session:
                if session.scalar(
                    select(AuthUserRecord.id)
                    .where(AuthUserRecord.role == AuthRole.OWNER.value)
                    .limit(1)
                ):
                    raise RuntimeError("First OWNER already exists")
                session.add(
                    AuthUserRecord(
                        id=owner_id,
                        normalized_login=normalized,
                        password_hash=password_hash,
                        role=AuthRole.OWNER.value,
                        state="ACTIVE",
                        bound_tailscale_login=identity,
                    )
                )
                session.flush()
                self._audit(
                    session,
                    event_type="OWNER_BOOTSTRAP",
                    outcome="SUCCEEDED",
                    actor_id=owner_id,
                    target_id=owner_id,
                    login=normalized,
                    tailscale_login=identity,
                    reason="LOCAL_INTERACTIVE_BOOTSTRAP",
                )
        except IntegrityError as exc:
            # Unique login and the partial unique OWNER index resolve racing
            # bootstrap attempts at the database boundary.
            raise RuntimeError("OWNER bootstrap lost a uniqueness race") from exc
        return owner_id

    def owner_exists(self) -> bool:
        """Read-only check used by the local bootstrap CLI before prompting."""

        with self.database.session() as session:
            return (
                session.scalar(
                    select(AuthUserRecord.id)
                    .where(AuthUserRecord.role == AuthRole.OWNER.value)
                    .limit(1)
                )
                is not None
            )

    @staticmethod
    def _owner_in_session(session, actor_id: str, tailscale_login: str) -> AuthUserRecord:
        actor = session.get(AuthUserRecord, actor_id)
        identity = normalize_login(tailscale_login)
        if (
            actor is None
            or actor.role != AuthRole.OWNER.value
            or actor.state != "ACTIVE"
            or not identity
            or actor.bound_tailscale_login != identity
        ):
            raise PermissionError("Active identity-bound OWNER authorization required")
        return actor

    @staticmethod
    def _safe_account(user: AuthUserRecord) -> dict[str, object]:
        state = {
            "PENDING_APPROVAL": AccountState.PENDING.value,
            "PROVISIONED": AccountState.PENDING.value,
            "ACTIVE": AccountState.ACTIVE.value,
            "REVOKED": AccountState.DISABLED.value,
            "LOCKED": AccountState.DISABLED.value,
        }.get(user.state, "UNKNOWN")
        return {
            "id": user.id,
            "login": user.normalized_login,
            "role": user.role,
            "state": state,
            "bound_tailscale_identity": user.bound_tailscale_login,
            "created_at": user.created_at.isoformat(),
            "updated_at": user.updated_at.isoformat(),
        }

    def list_accounts(self, *, actor_id: str, tailscale_login: str) -> list[dict[str, object]]:
        with self.database.session() as session:
            self._owner_in_session(session, actor_id, tailscale_login)
            users = session.scalars(
                select(AuthUserRecord).order_by(AuthUserRecord.created_at, AuthUserRecord.id)
            ).all()
            return [self._safe_account(user) for user in users]

    def create_invitation(
        self,
        *,
        actor_id: str,
        tailscale_login: str,
        login: str,
        role: str,
        bound_tailscale_login: str,
    ) -> EnrollmentIssue:
        normalized = normalize_login(login)
        bound_identity = normalize_login(bound_tailscale_login)
        if (
            not normalized
            or len(normalized) > 254
            or not bound_identity
            or len(bound_identity) > 254
        ):
            raise ValueError("A valid account login and Tailscale identity are required")
        if role not in {AuthRole.ADMIN.value}:
            raise ValueError("Only the explicitly allowed ADMIN role may be invited")
        secret = secrets.token_urlsafe(ENROLLMENT_SECRET_BYTES)
        secret_hash = _token_hash(secret)
        now = datetime.now(UTC)
        expires_at = now + ENROLLMENT_TTL
        # Pending accounts have no usable password. Enrollment sets one only
        # after the secret and expected Tailscale identity are both verified.
        unusable_password_hash = self.hash_password(secrets.token_urlsafe(48))
        user_id = new_id()
        try:
            with self.database.session() as session:
                actor = self._owner_in_session(session, actor_id, tailscale_login)
                user = AuthUserRecord(
                    id=user_id,
                    normalized_login=normalized,
                    password_hash=unusable_password_hash,
                    role=role,
                    state="PENDING_APPROVAL",
                    bound_tailscale_login=bound_identity,
                )
                session.add(user)
                session.flush()
                session.add(
                    AuthEnrollmentRecord(
                        user_id=user_id,
                        token_hash=secret_hash,
                        created_by_user_id=actor.id,
                        created_at=now,
                        expires_at=expires_at,
                    )
                )
                self._audit(
                    session,
                    event_type="ACCOUNT_CREATED",
                    outcome="SUCCEEDED",
                    actor_id=actor.id,
                    target_id=user_id,
                    login=normalized,
                    tailscale_login=bound_identity,
                    reason="ROLE_ADMIN;state=PENDING",
                )
                self._audit(
                    session,
                    event_type="ENROLLMENT_ISSUED",
                    outcome="SUCCEEDED",
                    actor_id=actor.id,
                    target_id=user_id,
                    login=normalized,
                    tailscale_login=bound_identity,
                    reason="TTL_15_MINUTES",
                )
                safe_account = self._safe_account(user)
        except IntegrityError as exc:
            raise ValueError("Account login is already provisioned") from exc
        return EnrollmentIssue(safe_account, secret, expires_at)

    def activate_enrollment(self, *, secret: str, password: str, tailscale_login: str) -> bool:
        identity = normalize_login(tailscale_login)
        if not identity or not isinstance(secret, str) or not 32 <= len(secret) <= 256:
            self._record_enrollment_failure(identity, "INVALID_ENROLLMENT")
            return False
        try:
            password_hash = self.hash_password(password)
        except ValueError:
            self._record_enrollment_failure(identity, "INVALID_PASSWORD")
            return False
        now = datetime.now(UTC)
        digest = _token_hash(secret)
        with self.database.session() as session:
            enrollment = session.scalar(
                select(AuthEnrollmentRecord).where(AuthEnrollmentRecord.token_hash == digest)
            )
            user = session.get(AuthUserRecord, enrollment.user_id) if enrollment else None
            failure = None
            if enrollment is None:
                failure = "INVALID_ENROLLMENT"
            elif enrollment.consumed_at is not None or enrollment.revoked_at is not None:
                failure = "ENROLLMENT_ALREADY_USED"
            elif enrollment.expires_at <= now:
                failure = "ENROLLMENT_EXPIRED"
            elif user is None or user.state != "PENDING_APPROVAL":
                failure = "ACCOUNT_NOT_PENDING"
            elif user.bound_tailscale_login != identity:
                failure = "IDENTITY_MISMATCH"
            if failure:
                self._audit(
                    session,
                    event_type="ENROLLMENT_FAILURE",
                    outcome="DENIED",
                    target_id=user.id if user else None,
                    login=user.normalized_login if user else None,
                    tailscale_login=identity or None,
                    reason=failure,
                )
                return False

            consumed = session.execute(
                update(AuthEnrollmentRecord)
                .where(
                    AuthEnrollmentRecord.id == enrollment.id,
                    AuthEnrollmentRecord.consumed_at.is_(None),
                    AuthEnrollmentRecord.revoked_at.is_(None),
                    AuthEnrollmentRecord.expires_at > now,
                )
                .values(consumed_at=now)
            )
            if consumed.rowcount != 1:
                self._audit(
                    session,
                    event_type="ENROLLMENT_FAILURE",
                    outcome="DENIED",
                    target_id=user.id,
                    login=user.normalized_login,
                    tailscale_login=identity,
                    reason="ENROLLMENT_REPLAY",
                )
                return False
            activated = session.execute(
                update(AuthUserRecord)
                .where(
                    AuthUserRecord.id == user.id,
                    AuthUserRecord.state == "PENDING_APPROVAL",
                    AuthUserRecord.bound_tailscale_login == identity,
                )
                .values(state="ACTIVE", password_hash=password_hash, updated_at=now)
            )
            if activated.rowcount != 1:
                # Raising rolls back the one-time consumption as well.
                raise RuntimeError("Enrollment account changed during activation")
            self._audit(
                session,
                event_type="ENROLLMENT_CONSUMED",
                outcome="SUCCEEDED",
                actor_id=user.id,
                target_id=user.id,
                login=user.normalized_login,
                tailscale_login=identity,
                reason="ACCOUNT_ACTIVATED",
            )
            self._audit(
                session,
                event_type="ACCOUNT_ACTIVATED",
                outcome="SUCCEEDED",
                actor_id=user.id,
                target_id=user.id,
                login=user.normalized_login,
                tailscale_login=identity,
                reason="OWNER_ISSUED_ENROLLMENT",
            )
            return True

    def _record_enrollment_failure(self, identity: str, reason: str) -> None:
        with self.database.session() as session:
            self._audit(
                session,
                event_type="ENROLLMENT_FAILURE",
                outcome="DENIED",
                tailscale_login=identity or None,
                reason=reason,
            )

    def disable_account(self, *, actor_id: str, tailscale_login: str, target_id: str) -> None:
        now = datetime.now(UTC)
        with self.database.session() as session:
            actor = self._owner_in_session(session, actor_id, tailscale_login)
            target = session.get(AuthUserRecord, target_id)
            if target is None:
                raise LookupError("Account not found")
            if target.role == AuthRole.OWNER.value:
                raise ValueError("The sole OWNER cannot be disabled")
            if target.state == "REVOKED":
                return
            target.state = "REVOKED"
            sessions = session.scalars(
                select(AuthSessionRecord).where(
                    AuthSessionRecord.user_id == target.id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
            ).all()
            for auth_session in sessions:
                auth_session.revoked_at = now
                self._audit(
                    session,
                    event_type="SESSION_REVOKED",
                    outcome="SUCCEEDED",
                    actor_id=actor.id,
                    target_id=target.id,
                    login=target.normalized_login,
                    tailscale_login=target.bound_tailscale_login,
                    reason="ACCOUNT_DISABLED",
                )
            session.execute(
                update(AuthEnrollmentRecord)
                .where(
                    AuthEnrollmentRecord.user_id == target.id,
                    AuthEnrollmentRecord.consumed_at.is_(None),
                    AuthEnrollmentRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            self._audit(
                session,
                event_type="ACCOUNT_DISABLED",
                outcome="SUCCEEDED",
                actor_id=actor.id,
                target_id=target.id,
                login=target.normalized_login,
                tailscale_login=target.bound_tailscale_login,
                reason="OWNER_ACTION",
            )

    def list_account_sessions(
        self, *, actor_id: str, tailscale_login: str, target_id: str
    ) -> list[dict[str, object]]:
        with self.database.session() as session:
            self._owner_in_session(session, actor_id, tailscale_login)
            target = session.get(AuthUserRecord, target_id)
            if target is None:
                raise LookupError("Account not found")
            records = session.scalars(
                select(AuthSessionRecord)
                .where(AuthSessionRecord.user_id == target_id)
                .order_by(AuthSessionRecord.created_at.desc())
            ).all()
            return [
                {
                    "id": item.id,
                    "created_at": item.created_at.isoformat(),
                    "last_seen_at": item.last_seen_at.isoformat(),
                    "idle_expires_at": item.idle_expires_at.isoformat(),
                    "absolute_expires_at": item.absolute_expires_at.isoformat(),
                    "revoked": item.revoked_at is not None,
                }
                for item in records
            ]

    def revoke_account_session(
        self, *, actor_id: str, tailscale_login: str, target_id: str, session_id: str
    ) -> bool:
        now = datetime.now(UTC)
        with self.database.session() as session:
            actor = self._owner_in_session(session, actor_id, tailscale_login)
            target = session.get(AuthUserRecord, target_id)
            record = session.get(AuthSessionRecord, session_id)
            if target is None or record is None or record.user_id != target_id:
                raise LookupError("Session not found")
            if record.revoked_at is not None:
                return False
            record.revoked_at = now
            self._audit(
                session,
                event_type="SESSION_REVOKED",
                outcome="SUCCEEDED",
                actor_id=actor.id,
                target_id=target.id,
                login=target.normalized_login,
                tailscale_login=target.bound_tailscale_login,
                reason="OWNER_SELECTED_SESSION",
            )
            return True

    def revoke_all_account_sessions(
        self, *, actor_id: str, tailscale_login: str, target_id: str
    ) -> int:
        now = datetime.now(UTC)
        with self.database.session() as session:
            actor = self._owner_in_session(session, actor_id, tailscale_login)
            target = session.get(AuthUserRecord, target_id)
            if target is None:
                raise LookupError("Account not found")
            changed = session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.user_id == target.id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            ).rowcount
            self._audit(
                session,
                event_type="SESSIONS_REVOKED_ALL",
                outcome="SUCCEEDED",
                actor_id=actor.id,
                target_id=target.id,
                login=target.normalized_login,
                tailscale_login=target.bound_tailscale_login,
                reason=f"COUNT_{changed or 0}",
            )
            return int(changed or 0)

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
