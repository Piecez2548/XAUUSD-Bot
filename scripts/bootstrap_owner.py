"""Create the first application OWNER from an interactive local terminal.

Run with ``.venv\\Scripts\\python.exe -m scripts.bootstrap_owner`` after
migrating the intended local database to Alembic head. Passwords are read
using getpass and are never accepted as command-line arguments or environment
values.
"""

from __future__ import annotations

import getpass
import sys

from config.settings import load_settings
from persistence.database import Database
from services.authentication import AuthenticationService


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("OWNER bootstrap requires an interactive local terminal.", file=sys.stderr)
        return 2

    settings = load_settings()
    database = Database(settings.database_url)
    try:
        database.require_auth_schema()
        auth = AuthenticationService(database, settings)
        if auth.owner_exists():
            print("An OWNER already exists; first-owner bootstrap is permanently closed.")
            database.dispose()
            return 1
    except Exception:
        database.dispose()
        print(
            "OWNER bootstrap unavailable; verify database migration and local database access.",
            file=sys.stderr,
        )
        return 1

    print("Create the one-time initial XAUUSD dashboard OWNER.")
    print("This command never prints or accepts a password as an argument.")
    if input('Type "BOOTSTRAP OWNER" to continue: ').strip() != "BOOTSTRAP OWNER":
        print("Bootstrap cancelled.")
        return 1

    login = input("OWNER login: ").strip()
    identity = input("Expected Tailscale login identity: ").strip()
    password = getpass.getpass("New OWNER password (12–1024 characters): ")
    confirmation = getpass.getpass("Confirm OWNER password: ")
    if password != confirmation:
        print("Passwords did not match.", file=sys.stderr)
        return 1

    try:
        owner_id = auth.bootstrap_owner(
            login=login,
            password=password,
            bound_tailscale_login=identity,
        )
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print(
            "OWNER bootstrap failed safely; verify database migration and local database access.",
            file=sys.stderr,
        )
        return 1
    finally:
        # Python strings cannot be reliably zeroized; avoid retaining references
        # beyond this local one-shot operation.
        password = ""
        confirmation = ""
        database.dispose()

    print(f"Initial OWNER created successfully (account id {owner_id}).")
    print("No password or enrollment secret was written to output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
