"""SQLAlchemy database boundary with SQLite hardening and PostgreSQL portability."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from persistence.orm import Base


def _sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        return None
    return Path(database_url.removeprefix(prefix))


def resolve_database_url(database_url: str, project_root: Path | None = None) -> str:
    """Return one deterministic URL for a SQLite database.

    Relative SQLite URLs are application-relative, never process-cwd-relative.
    This keeps control, API, live, and CLI/migration processes on the same
    database even when launched from different working directories.
    """

    path = _sqlite_path(database_url)
    if path is None:
        return database_url
    if not path.is_absolute() and project_root is not None:
        path = project_root.resolve() / path
    if path.is_absolute():
        path = path.resolve()
    return f"sqlite:///{path.as_posix()}"


class Database:
    def __init__(self, database_url: str, *, project_root: Path | None = None) -> None:
        self.database_url = resolve_database_url(database_url, project_root)
        path = _sqlite_path(self.database_url)
        if path is not None:
            path.mkdir(parents=True, exist_ok=True) if path.suffix == "" else path.parent.mkdir(
                parents=True, exist_ok=True
            )
        self.database_identity = hashlib.sha256(self.database_url.encode("utf-8")).hexdigest()[:16]

        connect_args = {"check_same_thread": False, "timeout": 30} if path is not None else {}
        self.engine: Engine = create_engine(
            self.database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def healthcheck(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        self.engine.dispose()
