# XAUUSD AI Trader — Phase 1.6 Live Trading Observatory

A local-first, read-only observability and analytics foundation for one broker-provided XAUUSD/gold instrument. Phase 1.6 adds a continuous serialized MT5 data engine while preserving the Phase 1.5 safety boundary: no AI, news, order APIs, or trade execution.

> **READ ONLY — ORDER EXECUTION DISABLED**

There are no BUY, SELL, CLOSE, or MODIFY endpoints or controls. The production code does not call `mt5.order_send`.

## What is implemented

- Existing Phase 1 MT5 connection, deterministic gold-symbol discovery, account/tick/candle/position reads, validation, and terminal report remain intact.
- SQLite persistence through SQLAlchemy and Alembic, designed for a future PostgreSQL URL.
- Typed, versioned domain events with an in-process bus, critical database subscriber, and failure-isolated optional subscribers.
- Account, market, position, risk, trade, decision, performance, health, event, CSV-export, and WebSocket read interfaces.
- Risk snapshots using broker tick size/value and observed stops; a missing stop makes aggregate risk explicitly unavailable.
- Pure trade analytics with transparent insufficient-sample behavior.
- Optional Telegram subscriber with bounded retries and reusable future lifecycle templates.
- Timestamped, non-overwriting SQLite backups.
- Option #2 audit-first React/Tailwind/Recharts dashboard with System Health, Risk Budget, Equity/Drawdown, Audit Timeline, Event Stream, Positions, Trades, and honest empty states.

AI inference, news/calendar ingestion, trade execution, and autonomous position management are **PLANNED**, not active.

## Prerequisites

- Windows with MetaTrader 5 Desktop installed and logged in
- Python 3.11+
- Node.js 20+

## Setup

```powershell
cd "D:\Project_001\Nexus-Project\XAUUSD Bot"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env

cd frontend
npm install
```

The safest MT5 configuration leaves all explicit credentials unset and uses the currently logged-in terminal. If credentials are required, `MT5_LOGIN`, `MT5_SERVER`, and `MT5_PASSWORD` must be supplied together. Set `TRADING_SYMBOL` only when deterministic discovery needs the broker's exact symbol.

## Commands

From the project root:

```powershell
# Unchanged Phase 1 live diagnostic (default)
python main.py
python main.py phase1

# Apply schema migrations
python main.py migrate

# Perform one read-only collection and persist it
python main.py observe

# Continuous read-only MT5 engine (Ctrl+C to stop)
python main.py live

# Persistent Telegram control plane (monitoring only; deny-by-default)
python main.py control

# Run the read API and WebSocket
python main.py server

# Create a timestamped SQLite backup
python main.py backup

# Explicit opt-in Telegram connectivity test
python main.py telegram-test
```

In another terminal:

```powershell
cd frontend
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` and `/ws` to `http://127.0.0.1:8000` during development.

## Status semantics

- `CONNECTED` / `ONLINE`: verified by the backend now or by a fresh successful observation.
- `UNKNOWN`: configured or historically present, but not currently verified.
- `DISCONNECTED` / `ERROR`: an explicit failure is known.
- `DISABLED`: intentionally off, including all trade execution in Phase 1.5.
- `PLANNED`: designed for a later phase but not implemented.

The dashboard never promotes `PLANNED` or `UNKNOWN` to healthy.

## Validation

```powershell
python -m pytest -q
python -m ruff check .
python -m compileall -q . -x '\\.venv'

cd frontend
npm run typecheck
npm run lint
npm test
npm run build
```

Only a successful `python main.py` or `python main.py observe` run against the user's logged-in MT5 terminal validates the live broker integration. Unit/API tests do not require MT5.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Database and event model](docs/DATABASE.md)
- [Analytics and risk formulas](docs/ANALYTICS.md)
- [Operations, backup, restore, and deployment](docs/OPERATIONS.md)
- [Security and read-only controls](docs/SECURITY.md)
- `DESIGN.md` records the shipped UI system after finish review.

## Phase boundary

Phase 1.6 is a Trading Observatory. It observes, persists, explains, exports, and notifies continuously. It does not decide trades, consume news, request orders, manage positions, or execute anything. Broker deal facts are reconciled idempotently; closed trades are reconstructed only when entry and exit facts are both verified.

## Phase 1.7 operations

Set `TELEGRAM_CONTROL_ENABLED=true`, `TELEGRAM_ALLOWED_CHAT_IDS`, and
`TELEGRAM_ALLOWED_USER_IDS` explicitly before running `python main.py control`.
Both identifiers must match; empty allowlists deny every command. The control
service uses Telegram long polling and fixed command handlers only. `/start`
starts the API and Live Data Engine; `/stop` leaves broker positions untouched.

After `npm run build` in `frontend`, the API serves the production dashboard at
`http://127.0.0.1:8000/` without requiring Vite. Optional Windows Task Scheduler
scripts are in `scripts/install_telegram_control_task.ps1`, with matching
uninstall/status scripts. Installing the task is always an explicit operator
action and starts only the control service.

Phase 1.7.1 keeps current positions and risk coherent by persisting each live
account/position/risk cycle atomically. Verified empty MT5 positions remove
closed tickets from current views; failed MT5 reads remain unavailable rather
than being interpreted as zero. Telegram and API responses expose observation
time, snapshot ID, and freshness.
