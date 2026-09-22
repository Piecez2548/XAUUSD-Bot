"""Timestamped, non-overwriting local SQLite backup foundation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class BackupError(RuntimeError):
    pass


def resolve_sqlite_path(database_url: str, project_root: Path) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        raise BackupError("built-in backup supports file-based SQLite databases only")
    path = Path(database_url.removeprefix(prefix))
    return path if path.is_absolute() else project_root / path


def create_database_backup(
    database_url: str,
    backup_directory: Path,
    *,
    project_root: Path,
    now: datetime | None = None,
) -> Path:
    source = resolve_sqlite_path(database_url, project_root)
    if not source.exists():
        raise BackupError(f"database does not exist: {source}")
    target_directory = (
        backup_directory if backup_directory.is_absolute() else project_root / backup_directory
    )
    target_directory.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    target = target_directory / f"trading_observatory_{timestamp}.db"
    if target.exists():
        raise BackupError(f"backup target already exists: {target}")
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
    return target
