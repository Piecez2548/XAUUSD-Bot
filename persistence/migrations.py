"""Programmatic Alembic migration entry point."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command
from persistence.database import resolve_database_url


def migrate_database(database_url: str, project_root: Path) -> None:
    config = Config(str(project_root / "alembic.ini"))
    resolved_url = resolve_database_url(database_url, project_root)
    if resolved_url.startswith("sqlite:///") and resolved_url != "sqlite:///:memory:":
        Path(resolved_url.removeprefix("sqlite:///" )).parent.mkdir(
            parents=True, exist_ok=True
        )
    config.set_main_option("sqlalchemy.url", resolved_url.replace("%", "%%"))
    command.upgrade(config, "head")
