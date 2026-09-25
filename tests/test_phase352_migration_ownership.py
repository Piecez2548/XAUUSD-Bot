from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from alembic import command
from api.app import create_app
from config.settings import Settings, load_settings
from persistence.database import AUTH_SCHEMA_MINIMUM_REVISION, AuthSchemaError, Database
from persistence.orm import (
    AUTH_SCHEMA_TABLE_NAMES,
    AuthUserRecord,
    ConfigurationVersionRecord,
)
from services.authentication import AuthenticationService

PRE_AUTH_REVISION = "20260924_0013"
CURRENT_HEAD_REVISION = "20260925_0019"
TAILSCALE = {
    "Tailscale-User-Login": "operator@example.test",
    "Origin": "https://dashboard.tailnet.test",
}


def private_settings() -> Settings:
    return Settings(
        remote_dashboard_mode=True,
        csrf_trusted_origins=("https://dashboard.tailnet.test",),
    )
PASSWORD = "Migration Ownership Test Password! 42"


def _config(database_path: Path) -> Config:
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def _revision_and_tables(database: Database) -> tuple[str | None, set[str]]:
    with database.engine.connect() as connection:
        tables = set(connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).scalars())
        if "alembic_version" not in tables:
            return None, tables
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        return revision, tables


def _stamp(database: Database, revision: str) -> None:
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (revision,)
        )


def test_startup_at_0013_does_not_create_auth_and_migration_then_initializes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "revision-0013.db"
    database = Database.for_test(f"sqlite:///{db_path.as_posix()}")
    database.create_schema()
    _stamp(database, PRE_AUTH_REVISION)
    try:
        with database.session() as session:
            session.add(
                ConfigurationVersionRecord(
                    version="legacy-preserved",
                    configuration={"preserve": True},
                    checksum="a" * 64,
                    active=True,
                )
            )
        with pytest.raises(RuntimeError, match="behind Alembic revision"):
            create_app(settings=private_settings(), database=database)

        revision, tables = _revision_and_tables(database)
        assert revision == PRE_AUTH_REVISION
        assert not (AUTH_SCHEMA_TABLE_NAMES & tables)

        command.upgrade(_config(db_path), AUTH_SCHEMA_MINIMUM_REVISION)
        revision, tables = _revision_and_tables(database)
        assert revision == AUTH_SCHEMA_MINIMUM_REVISION
        assert tables >= AUTH_SCHEMA_TABLE_NAMES
        with database.session() as session:
            preserved = session.query(ConfigurationVersionRecord).filter_by(
                version="legacy-preserved"
            ).one()
            assert preserved.configuration == {"preserve": True}
            assert preserved.checksum == "a" * 64

        app = create_app(settings=private_settings(), database=database)
        with TestClient(app, base_url="https://dashboard.tailnet.test") as client:
            assert client.get("/api/auth/session", headers=TAILSCALE).json() == {
                "authenticated": False
            }
            assert client.get("/api/system/health", headers=TAILSCALE).status_code == 401

            AuthenticationService(database, Settings()).create_user_for_admin(
                login="operator@example.test",
                password=PASSWORD,
                role="ADMIN",
                state="ACTIVE",
                bound_tailscale_login="operator@example.test",
            )
            login = client.post(
                "/api/auth/login",
                headers=TAILSCALE,
                json={"login": "operator@example.test", "password": PASSWORD},
            )
            assert login.status_code == 200
            assert client.get("/api/system/health", headers=TAILSCALE).status_code == 200
    finally:
        database.dispose()


def test_fresh_alembic_bootstrap_defers_auth_tables_until_0014(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-alembic.db"
    command.upgrade(_config(db_path), "20260921_0001")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, tables = _revision_and_tables(database)
        assert revision == "20260921_0001"
        assert not (AUTH_SCHEMA_TABLE_NAMES & tables)
    finally:
        database.dispose()


def test_fresh_migration_replay_reaches_head_with_auth_owned_through_0015(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fresh-head.db"
    command.upgrade(_config(db_path), "head")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, tables = _revision_and_tables(database)
        assert revision == CURRENT_HEAD_REVISION
        assert tables >= AUTH_SCHEMA_TABLE_NAMES
        inspector = inspect(database.engine)
        assert {"config_version", "config_hash"} <= {
            column["name"] for column in inspector.get_columns("shadow_decisions")
        }
        assert {
            "accounts",
            "market_snapshots",
            "shadow_decisions",
            "shadow_outcomes",
            "research_datasets",
            "research_robustness_runs",
            "forward_validation_sessions",
            "strategy_intelligence_records",
            "demo_execution_controls",
            "pair_zone_evaluations",
        } <= tables
        database.require_auth_schema()
    finally:
        database.dispose()


def test_migrated_0013_database_preserves_legacy_row_through_head(tmp_path: Path) -> None:
    db_path = tmp_path / "upgrade-0013-to-head.db"
    config = _config(db_path)
    command.upgrade(config, PRE_AUTH_REVISION)
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        pre_upgrade_tables = set(inspect(database.engine).get_table_names())
        assert pre_upgrade_tables.isdisjoint(AUTH_SCHEMA_TABLE_NAMES)
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO configuration_versions "
                "(id, version, timestamp, configuration, checksum, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "legacy-config-id",
                    "legacy-0013",
                    "2026-09-24 00:00:00",
                    '{"preserved":true}',
                    "b" * 64,
                    1,
                ),
            )

        command.upgrade(config, "head")
        with database.engine.connect() as connection:
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            row = connection.exec_driver_sql(
                "SELECT id, version, configuration, checksum, active "
                "FROM configuration_versions WHERE id = ?",
                ("legacy-config-id",),
            ).one()
        post_upgrade_tables = set(inspect(database.engine).get_table_names())
        assert revision == CURRENT_HEAD_REVISION
        assert pre_upgrade_tables <= post_upgrade_tables
        assert post_upgrade_tables - pre_upgrade_tables == (
            AUTH_SCHEMA_TABLE_NAMES
            | {"pair_zone_evaluations", "model_inference_evaluations"}
        )
        assert tuple(row) == (
            "legacy-config-id",
            "legacy-0013",
            '{"preserved":true}',
            "b" * 64,
            1,
        )
        database.require_auth_schema()
    finally:
        database.dispose()


def test_pair_zone_0015_0016_upgrade_downgrade_round_trip_preserves_existing_data(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pair-zone-0015-0016-round-trip.db"
    config = _config(db_path)
    command.upgrade(config, "20260924_0015")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, tables = _revision_and_tables(database)
        assert revision == "20260924_0015"
        assert "pair_zone_evaluations" not in tables
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO configuration_versions "
                "(id, version, timestamp, configuration, checksum, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "pre-pair-zone-config",
                    "pre-pair-zone-0015",
                    "2026-09-24 00:00:00",
                    '{"preserve":"through-pair-zone-migration"}',
                    "c" * 64,
                    1,
                ),
            )
    finally:
        database.dispose()

    command.upgrade(config, "20260924_0016")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, tables = _revision_and_tables(database)
        assert revision == "20260924_0016"
        assert "pair_zone_evaluations" in tables
        assert "ix_pair_zone_evaluations_generation" in {
            index["name"]
            for index in inspect(database.engine).get_indexes("pair_zone_evaluations")
        }
        with database.engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT version, configuration, checksum, active "
                "FROM configuration_versions WHERE id = ?",
                ("pre-pair-zone-config",),
            ).one()
        assert tuple(row) == (
            "pre-pair-zone-0015",
            '{"preserve":"through-pair-zone-migration"}',
            "c" * 64,
            1,
        )
    finally:
        database.dispose()

    command.downgrade(config, "20260924_0015")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, tables = _revision_and_tables(database)
        assert revision == "20260924_0015"
        assert "pair_zone_evaluations" not in tables
        with database.engine.connect() as connection:
            preserved = connection.exec_driver_sql(
                "SELECT configuration FROM configuration_versions WHERE id = ?",
                ("pre-pair-zone-config",),
            ).scalar_one()
        assert preserved == '{"preserve":"through-pair-zone-migration"}'
    finally:
        database.dispose()

    command.upgrade(config, "20260924_0016")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, tables = _revision_and_tables(database)
        assert revision == "20260924_0016"
        assert "pair_zone_evaluations" in tables
        with database.engine.connect() as connection:
            preserved = connection.exec_driver_sql(
                "SELECT configuration FROM configuration_versions WHERE id = ?",
                ("pre-pair-zone-config",),
            ).scalar_one()
        assert preserved == '{"preserve":"through-pair-zone-migration"}'
    finally:
        database.dispose()


def test_risk_snapshot_index_0016_0017_round_trip_preserves_existing_data(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "risk-index-0016-0017-round-trip.db"
    config = _config(db_path)
    command.upgrade(config, "20260924_0016")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO configuration_versions "
                "(id, version, timestamp, configuration, checksum, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "pre-risk-index-config",
                    "preserved-before-risk-index",
                    "2026-09-25 00:00:00",
                    '{"preserve":"unrelated-production-era-data"}',
                    "d" * 64,
                    1,
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO risk_snapshots "
                "(id, market_snapshot_id, timestamp, equity, balance, "
                "open_risk_percent, open_risk_amount, remaining_risk_percent, "
                "risk_per_position, daily_pnl, daily_realized_loss, drawdown_percent, "
                "max_trade_risk_percent, max_aggregate_risk_percent, "
                "open_positions_count, unbounded_positions_count, "
                "margin_usage_percent, free_margin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "risk-before-index",
                    None,
                    "2026-09-25 00:00:00",
                    10100.0,
                    10000.0,
                    0.25,
                    25.0,
                    5.75,
                    "[]",
                    5.0,
                    0.0,
                    0.1,
                    2.0,
                    6.0,
                    0,
                    0,
                    1.0,
                    10000.0,
                ),
            )
        revision, _tables = _revision_and_tables(database)
        assert revision == "20260924_0016"
        assert "ix_risk_snapshots_market_snapshot_id" not in {
            index["name"] for index in inspect(database.engine).get_indexes("risk_snapshots")
        }
    finally:
        database.dispose()

    command.upgrade(config, "20260925_0017")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, _tables = _revision_and_tables(database)
        assert revision == "20260925_0017"
        assert "ix_risk_snapshots_market_snapshot_id" in {
            index["name"] for index in inspect(database.engine).get_indexes("risk_snapshots")
        }
        with database.engine.connect() as connection:
            preserved = connection.exec_driver_sql(
                "SELECT version, configuration, checksum, active "
                "FROM configuration_versions WHERE id = ?",
                ("pre-risk-index-config",),
            ).one()
        assert tuple(preserved) == (
            "preserved-before-risk-index",
            '{"preserve":"unrelated-production-era-data"}',
            "d" * 64,
            1,
        )
        with database.engine.connect() as connection:
            preserved_risk = connection.exec_driver_sql(
                "SELECT id, equity, balance, open_risk_percent, free_margin "
                "FROM risk_snapshots WHERE id = ?",
                ("risk-before-index",),
            ).one()
        assert tuple(preserved_risk) == (
            "risk-before-index",
            10100.0,
            10000.0,
            0.25,
            10000.0,
        )
    finally:
        database.dispose()

    command.downgrade(config, "20260924_0016")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, _tables = _revision_and_tables(database)
        assert revision == "20260924_0016"
        assert "ix_risk_snapshots_market_snapshot_id" not in {
            index["name"] for index in inspect(database.engine).get_indexes("risk_snapshots")
        }
        with database.engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT configuration FROM configuration_versions WHERE id = ?",
                    ("pre-risk-index-config",),
                ).scalar_one()
                == '{"preserve":"unrelated-production-era-data"}'
            )
            assert connection.exec_driver_sql(
                "SELECT equity, balance FROM risk_snapshots WHERE id = ?",
                ("risk-before-index",),
            ).one() == (10100.0, 10000.0)
    finally:
        database.dispose()

    command.upgrade(config, "20260925_0017")
    database = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        revision, _tables = _revision_and_tables(database)
        assert revision == "20260925_0017"
        assert "ix_risk_snapshots_market_snapshot_id" in {
            index["name"] for index in inspect(database.engine).get_indexes("risk_snapshots")
        }
    finally:
        database.dispose()


def test_historical_revision_boundaries_preserve_schema_ownership(tmp_path: Path) -> None:
    db_path = tmp_path / "historical-boundaries.db"
    config = _config(db_path)
    database = Database(f"sqlite:///{db_path.as_posix()}")

    def tables() -> set[str]:
        return set(inspect(database.engine).get_table_names())

    def columns(table_name: str) -> set[str]:
        return {column["name"] for column in inspect(database.engine).get_columns(table_name)}

    try:
        command.upgrade(config, "20260921_0001")
        current = tables()
        assert {
            "accounts", "account_snapshots", "symbols", "candles", "market_snapshots",
            "positions", "position_snapshots", "trades", "trade_events", "ai_decisions",
            "risk_snapshots", "performance_snapshots", "system_events", "system_health",
            "configuration_versions", "model_versions", "prompt_versions",
        } <= current
        assert current.isdisjoint(
            {
                "candle_cursors", "broker_deals", "history_cursors", "control_audit",
                "shadow_decisions", "shadow_outcomes", "strategy_activations",
                "research_datasets", "research_robustness_runs",
                "forward_validation_sessions", "strategy_intelligence_records",
                "demo_execution_controls", *AUTH_SCHEMA_TABLE_NAMES,
            }
        )
        assert not {
            "positions_observed_successfully", "open_position_count"
        } & columns("market_snapshots")

        command.upgrade(config, "20260922_0002")
        assert {"candle_cursors", "broker_deals", "history_cursors"} <= tables()
        assert "control_audit" not in tables()

        command.upgrade(config, "20260922_0003")
        assert "control_audit" in tables()
        assert not {
            "positions_observed_successfully", "open_position_count"
        } & columns("market_snapshots")
        assert "shadow_decisions" not in tables()

        command.upgrade(config, "20260922_0004")
        assert {
            "positions_observed_successfully", "open_position_count"
        } <= columns("market_snapshots")
        assert "shadow_decisions" not in tables()

        command.upgrade(config, "20260922_0005")
        assert "shadow_decisions" in tables()
        assert "shadow_outcomes" not in tables()
        assert not {"config_version", "config_hash"} & columns("shadow_decisions")

        command.upgrade(config, "20260922_0006")
        assert "shadow_outcomes" in tables()
        assert "strategy_activations" not in tables()

        command.upgrade(config, "20260922_0007")
        assert {"config_version", "config_hash"} <= columns("shadow_decisions")
        assert "strategy_activations" in tables()
        assert "research_datasets" not in tables()

        command.upgrade(config, "20260922_0008")
        assert {
            "research_datasets", "research_candles", "research_runs",
            "research_decisions", "research_outcomes", "research_metrics",
        } <= tables()
        assert "research_robustness_runs" not in tables()

        command.upgrade(config, "20260922_0009")
        assert "research_robustness_runs" in tables()
        assert "forward_validation_sessions" not in tables()

        command.upgrade(config, "20260922_0010")
        assert {
            "forward_validation_sessions", "forward_validation_signals",
            "forward_validation_trades",
        } <= tables()
        assert "strategy_intelligence_records" not in tables()

        command.upgrade(config, "20260923_0011")
        assert "strategy_intelligence_records" in tables()
        assert not {
            "intelligence_version", "intelligence_runtime_version",
            "pair_zone_event_id", "forward_trade_id",
        } & columns("strategy_intelligence_records")
        assert "demo_execution_controls" not in tables()

        command.upgrade(config, "20260924_0012")
        assert {
            "intelligence_version", "intelligence_runtime_version",
            "pair_zone_event_id", "forward_trade_id",
        } <= columns("strategy_intelligence_records")
        assert "demo_execution_controls" not in tables()

        command.upgrade(config, "20260924_0013")
        assert {"demo_execution_controls", "demo_execution_records"} <= tables()
        assert tables().isdisjoint(AUTH_SCHEMA_TABLE_NAMES)

        command.upgrade(config, "20260924_0014")
        assert {"auth_users", "auth_sessions", "auth_audit_events"} <= tables()
        assert "auth_enrollments" not in tables()
        with pytest.raises(AuthSchemaError, match="behind Alembic revision"):
            database.require_auth_schema()

        command.upgrade(config, "20260924_0015")
        assert tables() >= AUTH_SCHEMA_TABLE_NAMES
        assert database.require_auth_schema() is None
    finally:
        database.dispose()


def test_historical_bootstraps_do_not_consult_current_orm_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persistence.orm import Base

    def reject_current_metadata(*_args, **_kwargs) -> None:
        raise AssertionError("historical migration consulted current ORM metadata")

    monkeypatch.setattr(Base.metadata, "create_all", reject_current_metadata)
    migration_paths = [
        Path("alembic/versions/20260921_0001_observatory_schema.py"),
        Path("alembic/versions/20260922_0002_live_engine.py"),
        Path("alembic/versions/20260922_0003_control_audit.py"),
    ]
    for path in migration_paths:
        source = path.read_text(encoding="utf-8")
        assert "persistence.orm" not in source
        assert "Base.metadata" not in source

    config = _config(tmp_path / "frozen-bootstrap.db")
    command.upgrade(config, "20260922_0003")


def test_0015_test_schema_keeps_auth_tables_valid(tmp_path: Path) -> None:
    database = Database.for_test(f"sqlite:///{(tmp_path / 'revision-0015.db').as_posix()}")
    database.create_test_schema()
    try:
        app = create_app(settings=private_settings(), database=database)
        with TestClient(app, base_url="https://dashboard.tailnet.test") as client:
            assert client.get("/api/auth/session", headers=TAILSCALE).json() == {
                "authenticated": False
            }
        revision, tables = _revision_and_tables(database)
        assert revision == AUTH_SCHEMA_MINIMUM_REVISION
        assert tables >= AUTH_SCHEMA_TABLE_NAMES
    finally:
        database.dispose()


@pytest.mark.parametrize("revision", [None, PRE_AUTH_REVISION])
def test_private_app_fails_closed_when_auth_tables_are_missing(
    tmp_path: Path, revision: str | None
) -> None:
    database = Database(f"sqlite:///{(tmp_path / f'missing-{revision}.db').as_posix()}")
    database.create_schema()
    if revision is not None:
        _stamp(database, revision)
    try:
        with pytest.raises(RuntimeError, match="Alembic|auth schema"):
            create_app(settings=private_settings(), database=database)
        _, tables = _revision_and_tables(database)
        assert not (AUTH_SCHEMA_TABLE_NAMES & tables)
    finally:
        database.dispose()


def test_private_app_fails_closed_on_partial_auth_schema(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'partial.db').as_posix()}")
    database.create_schema()
    _stamp(database, AUTH_SCHEMA_MINIMUM_REVISION)
    AuthUserRecord.__table__.create(database.engine)
    try:
        with pytest.raises(RuntimeError, match="auth schema is incomplete"):
            create_app(settings=private_settings(), database=database)
        _, tables = _revision_and_tables(database)
        assert tables.isdisjoint(AUTH_SCHEMA_TABLE_NAMES - {"auth_users"})
    finally:
        database.dispose()


def test_private_app_fails_closed_when_database_is_unavailable(tmp_path: Path) -> None:
    unavailable_path = tmp_path / "unavailable.sqlite"
    unavailable_path.mkdir()
    database = Database(f"sqlite:///{unavailable_path.as_posix()}")
    try:
        with pytest.raises(RuntimeError, match="auth schema initialization failed"):
            create_app(settings=private_settings(), database=database)
    finally:
        database.dispose()


@pytest.mark.parametrize("revision", [PRE_AUTH_REVISION, "unknown_revision"])
def test_test_schema_helper_refuses_existing_revision(
    tmp_path: Path, revision: str
) -> None:
    database = Database.for_test(
        f"sqlite:///{(tmp_path / f'no-restamp-{revision}.db').as_posix()}"
    )
    database.create_schema()
    _stamp(database, revision)
    try:
        with pytest.raises(AuthSchemaError, match="fresh disposable"):
            database.create_test_schema()
    finally:
        database.dispose()


def test_normal_database_cannot_invoke_synthetic_test_schema(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'normal.db').as_posix()}")
    try:
        with pytest.raises(AuthSchemaError, match="Database.for_test"):
            database.create_test_schema()
        _, tables = _revision_and_tables(database)
        assert "alembic_version" not in tables
        assert not (AUTH_SCHEMA_TABLE_NAMES & tables)
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "database_url",
    ["sqlite:///data/production-like.db", "postgresql://localhost/not-a-test-db"],
)
def test_test_database_factory_rejects_non_disposable_targets(database_url: str) -> None:
    with pytest.raises(AuthSchemaError, match="disposable|temporary directory"):
        Database.for_test(database_url)


def test_pytest_environment_is_independent_of_operator_dotenv() -> None:
    assert load_settings().remote_dashboard_mode is False


def _create_auth_schema_fixture(database: Database, defect: str | None = None) -> None:
    user_fields = [
        ("id", "VARCHAR(36) PRIMARY KEY"),
        ("normalized_login", "VARCHAR(254) NOT NULL"),
        ("password_hash", "VARCHAR(512) NOT NULL"),
        ("role", "VARCHAR(16) NOT NULL"),
        ("state", "VARCHAR(24) NOT NULL"),
        ("bound_tailscale_login", "VARCHAR(254) NOT NULL"),
        ("created_at", "DATETIME NOT NULL"),
        ("updated_at", "DATETIME NOT NULL"),
    ]
    session_fields = [
        ("id", "VARCHAR(36) PRIMARY KEY"),
        ("user_id", "VARCHAR(36) NOT NULL"),
        ("token_hash", "VARCHAR(64) NOT NULL"),
        ("created_at", "DATETIME NOT NULL"),
        ("last_seen_at", "DATETIME NOT NULL"),
        ("idle_expires_at", "DATETIME NOT NULL"),
        ("absolute_expires_at", "DATETIME NOT NULL"),
        ("revoked_at", "DATETIME"),
    ]
    audit_fields = [
        ("id", "VARCHAR(36) PRIMARY KEY"),
        ("timestamp", "DATETIME NOT NULL"),
        ("event_type", "VARCHAR(32) NOT NULL"),
        ("actor_user_id", "VARCHAR(36)"),
        ("user_id", "VARCHAR(36)"),
        ("normalized_login", "VARCHAR(254)"),
        ("tailscale_login", "VARCHAR(254)"),
        ("session_id", "VARCHAR(36)"),
        ("outcome", "VARCHAR(24) NOT NULL"),
        ("reason", "VARCHAR(64)"),
    ]
    if defect == "missing_password_hash":
        user_fields = [field for field in user_fields if field[0] != "password_hash"]
    if defect == "missing_token_hash":
        session_fields = [field for field in session_fields if field[0] != "token_hash"]
    if defect == "malformed_audit":
        audit_fields = [field for field in audit_fields if field[0] != "event_type"]
    user_unique = (
        "" if defect == "user_login_not_unique" else ", UNIQUE(normalized_login)"
    )
    session_unique = (
        ""
        if defect in {"session_token_not_unique", "missing_token_hash"}
        else ", UNIQUE(token_hash)"
    )
    session_fk = (
        "" if defect == "session_user_fk_missing" else
        ", FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE"
    )
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (AUTH_SCHEMA_MINIMUM_REVISION,),
        )
        connection.exec_driver_sql(
            "CREATE TABLE auth_users ("
            + ", ".join(f"{name} {kind}" for name, kind in user_fields)
            + user_unique
            + ")"
        )
        connection.exec_driver_sql(
            "CREATE TABLE auth_sessions ("
            + ", ".join(f"{name} {kind}" for name, kind in session_fields)
            + session_unique
            + session_fk
            + ")"
        )
        connection.exec_driver_sql(
            "CREATE TABLE auth_audit_events ("
            + ", ".join(f"{name} {kind}" for name, kind in audit_fields)
            + ", FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE SET NULL)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_auth_users_bound_tailscale_login "
            "ON auth_users(bound_tailscale_login)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_auth_users_single_owner ON auth_users(role) "
            "WHERE role = 'OWNER'"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_auth_sessions_user_id ON auth_sessions(user_id)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE auth_enrollments ("
            "id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, "
            "token_hash VARCHAR(64) NOT NULL UNIQUE, created_by_user_id VARCHAR(36) NOT NULL, "
            "created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL, "
            "consumed_at DATETIME, revoked_at DATETIME, "
            "FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE, "
            "FOREIGN KEY(created_by_user_id) REFERENCES auth_users(id) ON DELETE RESTRICT)"
        )


@pytest.mark.parametrize(
    "defect",
    [
        "missing_password_hash",
        "missing_token_hash",
        "malformed_audit",
        "user_login_not_unique",
        "session_token_not_unique",
        "session_user_fk_missing",
    ],
)
def test_private_startup_rejects_malformed_auth_schema(
    tmp_path: Path, defect: str
) -> None:
    database = Database.for_test(f"sqlite:///{(tmp_path / f'{defect}.db').as_posix()}")
    _create_auth_schema_fixture(database, defect)
    try:
        with pytest.raises(RuntimeError, match="auth schema is incompatible"):
            create_app(settings=private_settings(), database=database)
    finally:
        database.dispose()


def test_private_startup_accepts_known_descendant_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database.for_test(f"sqlite:///{(tmp_path / 'descendant.db').as_posix()}")
    database.create_test_schema()
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num='20260924_0015'"
        )

    class _FutureScript:
        @staticmethod
        def get_revision(revision: str):
            return revision if revision == "20260924_0015" else None

        @staticmethod
        def iterate_revisions(upper: str, lower: str):
            assert (upper, lower) == ("20260924_0015", AUTH_SCHEMA_MINIMUM_REVISION)
            return iter(("20260924_0015", AUTH_SCHEMA_MINIMUM_REVISION))

    monkeypatch.setattr(
        "persistence.database.ScriptDirectory.from_config",
        lambda _config: _FutureScript(),
    )
    try:
        database.require_auth_schema()
    finally:
        database.dispose()
