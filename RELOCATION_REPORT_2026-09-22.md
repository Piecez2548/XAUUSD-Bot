# XAUUSD Bot Relocation Report

The XAUUSD trading-observatory project was moved from:

`D:\Project_001\Nexus-Project\Nexus\xauusd-ai-trader`

to:

`D:\Project_001\Nexus-Project\XAUUSD Bot`

The original source directory no longer exists. The destination contains the application source, `.venv`, frontend, reports, logs, backups, and the existing SQLite database. No database rows were deleted.

## Verification

- `main.py`: present
- `.env`: present
- `.venv\Scripts\python.exe`: present
- `data\trading_observatory.db`: present (46,166,016 bytes at verification)
- `data\process_registry.json`: valid and updated to the new absolute paths
- Database healthcheck: passed
- Latest shadow decision: readable from the moved database
- Backend pytest: 128 passed, 2 warnings
- Ruff: passed
- compileall: passed
- Frontend Vitest: 2 files / 4 tests passed
- Frontend TypeScript: passed
- Frontend ESLint: passed
- Frontend production build: passed

Runtime processes were stopped before the move so no process retained the old path. The registry is marked `STOPPED`; start the runtime from the new folder when performing live acceptance.
