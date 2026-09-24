"""SQLAlchemy database boundary with SQLite hardening and PostgreSQL portability."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from persistence.orm import AUTH_SCHEMA_TABLE_NAMES, Base

AUTH_SCHEMA_MINIMUM_REVISION = "20260924_0014"


class AuthSchemaError(RuntimeError):
    """The private application schema is absent, partial, or behind Alembic."""


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
        self._disposable_test_database = False
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

    @classmethod
    def for_test(
        cls, database_url: str, *, project_root: Path | None = None
    ) -> Database:
        """Create an explicitly disposable SQLite database for tests only."""

        resolved_url = resolve_database_url(database_url, project_root)
        if resolved_url == "sqlite:///:memory:":
            disposable = True
        else:
            path = _sqlite_path(resolved_url)
            if path is None:
                raise AuthSchemaError("Test databases must use disposable SQLite storage")
            try:
                path.resolve().relative_to(Path(gettempdir()).resolve())
                disposable = True
            except ValueError:
                disposable = False
        if not disposable:
            raise AuthSchemaError(
                "Test databases must be located under the operating-system temporary directory"
            )
        database = cls(resolved_url, project_root=project_root)
        database._disposable_test_database = True
        return database

    @staticmethod
    def _configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def create_schema(self) -> None:
        """Preserve legacy bootstrapping without creating migration-owned auth tables."""

        tables = [
            table
            for table in Base.metadata.sorted_tables
            if table.name not in AUTH_SCHEMA_TABLE_NAMES
        ]
        Base.metadata.create_all(self.engine, tables=tables)

    def create_test_schema(self) -> None:
        """Explicit full-schema bootstrap and revision marker for disposable tests only."""

        if not self._disposable_test_database:
            raise AuthSchemaError(
                "Synthetic auth schema creation requires Database.for_test()"
            )
        with self.engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            if table_names:
                raise AuthSchemaError(
                    "Test schema helper only initializes a fresh disposable database"
                )

        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                (AUTH_SCHEMA_MINIMUM_REVISION,),
            )

    def require_auth_schema(self) -> None:
        """Require auth tables installed by Alembic before private app startup."""

        try:
            with self.engine.connect() as connection:
                inspector = inspect(connection)
                table_names = set(inspector.get_table_names())
                if "alembic_version" not in table_names:
                    raise AuthSchemaError(
                        "Alembic revision table is missing; run the approved database migration"
                    )
                revisions = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalars().all()
                if len(revisions) != 1:
                    raise AuthSchemaError(
                        "Private dashboard requires one current Alembic revision"
                    )
                current_revision = revisions[0]
                migration_root = Path(__file__).resolve().parents[1]
                script = ScriptDirectory.from_config(
                    Config(str(migration_root / "alembic.ini"))
                )
                if script.get_revision(current_revision) is None:
                    raise AuthSchemaError(
                        "Database Alembic revision is unknown to this application"
                    )
                if current_revision != AUTH_SCHEMA_MINIMUM_REVISION:
                    try:
                        path = list(
                            script.iterate_revisions(
                                current_revision, AUTH_SCHEMA_MINIMUM_REVISION
                            )
                        )
                    except Exception:
                        path = []
                    if not path:
                        raise AuthSchemaError(
                            "Private dashboard database is behind Alembic revision "
                            f"{AUTH_SCHEMA_MINIMUM_REVISION}"
                        )
                missing = AUTH_SCHEMA_TABLE_NAMES - table_names
                if missing:
                    raise AuthSchemaError(
                        "Private dashboard auth schema is incomplete; missing: "
                        + ", ".join(sorted(missing))
                    )
                for table_name in sorted(AUTH_SCHEMA_TABLE_NAMES):
                    actual_columns = {
                        column["name"] for column in inspector.get_columns(table_name)
                    }
                    expected_columns = set(Base.metadata.tables[table_name].columns.keys())
                    missing_columns = expected_columns - actual_columns
                    if missing_columns:
                        raise AuthSchemaError(
                            f"Private dashboard auth schema is incompatible; {table_name} "
                            "is missing required columns: "
                            + ", ".join(sorted(missing_columns))
                        )

                for table_name, column_name in (
                    ("auth_users", "normalized_login"),
                    ("auth_sessions", "token_hash"),
                ):
                    unique_columns = {
                        tuple(constraint.get("column_names") or ())
                        for constraint in inspector.get_unique_constraints(table_name)
                    }
                    unique_columns.update(
                        tuple(index.get("column_names") or ())
                        for index in inspector.get_indexes(table_name)
                        if index.get("unique")
                    )
                    if (column_name,) not in unique_columns:
                        raise AuthSchemaError(
                            f"Private dashboard auth schema is incompatible; "
                            f"{table_name}.{column_name} must be unique"
                        )

                session_user_fk = any(
                    tuple(foreign_key.get("constrained_columns") or ()) == ("user_id",)
                    and foreign_key.get("referred_table") == "auth_users"
                    and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
                    for foreign_key in inspector.get_foreign_keys("auth_sessions")
                )
                if not session_user_fk:
                    raise AuthSchemaError(
                        "Private dashboard auth schema is incompatible; "
                        "auth_sessions.user_id must reference auth_users.id"
                    )
        except AuthSchemaError:
            raise
        except Exception as exc:
            raise AuthSchemaError(
                "Private dashboard auth schema could not be verified; database unavailable"
            ) from exc

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
