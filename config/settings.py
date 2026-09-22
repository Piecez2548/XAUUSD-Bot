"""Environment-backed settings for the read-only MT5 application."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CANDLE_COUNTS: dict[str, int] = {
    "M5": 1_000,
    "M15": 1_000,
    "H1": 500,
    "H4": 500,
}


class ConfigurationError(ValueError):
    """Raised when environment settings are incomplete or invalid."""


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _positive_int(name: str, default: int) -> int:
    value = _optional_int(name)
    result = default if value is None else value
    if result <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return result


def _nonnegative_int(name: str, default: int) -> int:
    value = _optional_int(name)
    result = default if value is None else value
    if result < 0:
        raise ConfigurationError(f"{name} cannot be negative")
    return result


def _ratio(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        result = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not 0 < result <= 1:
        raise ConfigurationError(f"{name} must be greater than 0 and at most 1")
    return result


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _origins(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    origins = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not origins:
        raise ConfigurationError(f"{name} must contain at least one origin")
    return origins


def _identifiers(name: str) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Passwords are excluded from the dataclass representation."""

    trading_symbol: str | None = None
    mt5_terminal_path: str | None = None
    mt5_auto_launch: bool = False
    mt5_startup_timeout_seconds: float = 30.0
    mt5_login: int | None = None
    mt5_server: str | None = None
    mt5_password: str | None = field(default=None, repr=False)
    candle_counts: dict[str, int] = field(default_factory=lambda: DEFAULT_CANDLE_COUNTS.copy())
    minimum_candle_ratio: float = 0.8
    log_level: str = "INFO"
    log_directory: Path = Path("logs")
    database_url: str = "sqlite:///data/trading_observatory.db"
    backup_directory: Path = Path("backups")
    telegram_enabled: bool = False
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)
    telegram_timeout_seconds: float = 5.0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    dashboard_public_url: str | None = None
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    max_trade_risk_percent: float = 2.0
    max_aggregate_risk_percent: float = 6.0
    shadow_engine_enabled: bool = True
    shadow_strategy: str = "baseline_v1"
    shadow_decision_timeframe: str = "M5"
    shadow_min_rr: float = 2.0
    shadow_max_spread_points: int = 100
    shadow_notify_signals: bool = True
    shadow_notify_no_trade: bool = False
    shadow_outcome_horizon_bars: int = 12
    forward_shadow_enabled: bool = False
    forward_shadow_rr: float = 2.0
    live_tick_interval_seconds: float = 1.0
    live_account_interval_seconds: float = 5.0
    live_candle_interval_seconds: float = 5.0
    live_history_interval_seconds: float = 30.0
    live_candle_lookback: int = 100
    mt5_reconnect_initial_seconds: float = 1.0
    mt5_reconnect_max_seconds: float = 30.0
    data_stale_tick_seconds: float = 10.0
    data_stale_account_seconds: float = 30.0
    data_stale_position_seconds: float = 30.0
    websocket_tick_throttle_ms: int = 500
    telegram_control_enabled: bool = False
    telegram_allowed_chat_ids: tuple[str, ...] = ()
    telegram_allowed_user_ids: tuple[str, ...] = ()
    telegram_control_poll_seconds: float = 5.0
    telegram_control_rate_limit_seconds: float = 3.0
    supervisor_max_restarts: int = 3
    supervisor_restart_window_seconds: float = 600.0
    supervisor_operation_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        supplied = (self.mt5_login, self.mt5_server, self.mt5_password)
        if any(value is not None for value in supplied) and not all(
            value is not None for value in supplied
        ):
            raise ConfigurationError(
                "MT5_LOGIN, MT5_SERVER, and MT5_PASSWORD must all be set "
                "when using explicit credentials"
            )
        if self.telegram_timeout_seconds <= 0:
            raise ConfigurationError("TELEGRAM_TIMEOUT_SECONDS must be greater than zero")
        if self.mt5_startup_timeout_seconds <= 0:
            raise ConfigurationError("MT5_STARTUP_TIMEOUT_SECONDS must be greater than zero")
        if self.max_trade_risk_percent <= 0:
            raise ConfigurationError("MAX_TRADE_RISK_PERCENT must be greater than zero")
        if self.max_aggregate_risk_percent <= 0:
            raise ConfigurationError("MAX_AGGREGATE_RISK_PERCENT must be greater than zero")
        if self.max_trade_risk_percent > self.max_aggregate_risk_percent:
            raise ConfigurationError(
                "MAX_TRADE_RISK_PERCENT cannot exceed MAX_AGGREGATE_RISK_PERCENT"
            )
        if self.shadow_decision_timeframe != "M5":
            raise ConfigurationError("SHADOW_DECISION_TIMEFRAME must be M5")
        if not self.shadow_strategy.strip():
            raise ConfigurationError("SHADOW_STRATEGY must not be empty")
        if self.shadow_min_rr <= 0:
            raise ConfigurationError("SHADOW_MIN_RR must be greater than zero")
        if self.shadow_max_spread_points < 0:
            raise ConfigurationError("SHADOW_MAX_SPREAD_POINTS cannot be negative")
        if self.shadow_outcome_horizon_bars <= 0:
            raise ConfigurationError("SHADOW_OUTCOME_HORIZON_BARS must be greater than zero")
        if self.forward_shadow_rr <= 0:
            raise ConfigurationError("FORWARD_SHADOW_RR must be greater than zero")
        positive = {
            "LIVE_TICK_INTERVAL_SECONDS": self.live_tick_interval_seconds,
            "LIVE_ACCOUNT_INTERVAL_SECONDS": self.live_account_interval_seconds,
            "LIVE_CANDLE_INTERVAL_SECONDS": self.live_candle_interval_seconds,
            "LIVE_HISTORY_INTERVAL_SECONDS": self.live_history_interval_seconds,
            "MT5_RECONNECT_INITIAL_SECONDS": self.mt5_reconnect_initial_seconds,
            "MT5_RECONNECT_MAX_SECONDS": self.mt5_reconnect_max_seconds,
            "DATA_STALE_TICK_SECONDS": self.data_stale_tick_seconds,
            "DATA_STALE_ACCOUNT_SECONDS": self.data_stale_account_seconds,
            "DATA_STALE_POSITION_SECONDS": self.data_stale_position_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
        if self.mt5_reconnect_max_seconds < self.mt5_reconnect_initial_seconds:
            raise ConfigurationError(
                "MT5_RECONNECT_MAX_SECONDS cannot be less than MT5_RECONNECT_INITIAL_SECONDS"
            )
        if self.live_candle_lookback < 2:
            raise ConfigurationError("LIVE_CANDLE_LOOKBACK must be at least 2")
        if self.websocket_tick_throttle_ms < 0:
            raise ConfigurationError("LIVE_WEBSOCKET_TICK_THROTTLE_MS cannot be negative")
        if self.telegram_control_poll_seconds <= 0:
            raise ConfigurationError("TELEGRAM_CONTROL_POLL_SECONDS must be greater than zero")
        if self.telegram_control_rate_limit_seconds <= 0:
            raise ConfigurationError(
                "TELEGRAM_CONTROL_RATE_LIMIT_SECONDS must be greater than zero"
            )
        if self.supervisor_max_restarts < 0:
            raise ConfigurationError("SUPERVISOR_MAX_RESTARTS cannot be negative")
        if self.supervisor_restart_window_seconds <= 0:
            raise ConfigurationError("SUPERVISOR_RESTART_WINDOW_SECONDS must be greater than zero")
        if self.supervisor_operation_timeout_seconds <= 0:
            raise ConfigurationError(
                "SUPERVISOR_OPERATION_TIMEOUT_SECONDS must be greater than zero"
            )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings from the environment, optionally loading a dotenv file first."""

    load_dotenv(dotenv_path=env_file, override=False)
    counts = {
        timeframe: _positive_int(f"CANDLES_{timeframe}", default)
        for timeframe, default in DEFAULT_CANDLE_COUNTS.items()
    }
    symbol = os.getenv("TRADING_SYMBOL")
    terminal_path = os.getenv("MT5_TERMINAL_PATH")
    server = os.getenv("MT5_SERVER")
    password = os.getenv("MT5_PASSWORD")

    return Settings(
        trading_symbol=symbol.strip() if symbol and symbol.strip() else None,
        mt5_terminal_path=(
            terminal_path.strip() if terminal_path and terminal_path.strip() else None
        ),
        mt5_auto_launch=_boolean("MT5_AUTO_LAUNCH", False),
        mt5_startup_timeout_seconds=float(os.getenv("MT5_STARTUP_TIMEOUT_SECONDS", "30")),
        mt5_login=_optional_int("MT5_LOGIN"),
        mt5_server=server.strip() if server and server.strip() else None,
        mt5_password=password if password else None,
        candle_counts=counts,
        minimum_candle_ratio=_ratio("MINIMUM_CANDLE_RATIO", 0.8),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_directory=Path(os.getenv("LOG_DIRECTORY", "logs")),
        database_url=os.getenv("DATABASE_URL", "sqlite:///data/trading_observatory.db"),
        backup_directory=Path(os.getenv("BACKUP_DIRECTORY", "backups")),
        telegram_enabled=_boolean("TELEGRAM_ENABLED"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        telegram_timeout_seconds=float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "5")),
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        api_port=_positive_int("API_PORT", 8000),
        dashboard_public_url=os.getenv("DASHBOARD_PUBLIC_URL") or None,
        cors_origins=_origins(
            "CORS_ORIGINS",
            ("http://localhost:5173", "http://127.0.0.1:5173"),
        ),
        max_trade_risk_percent=float(os.getenv("MAX_TRADE_RISK_PERCENT", "2")),
        max_aggregate_risk_percent=float(os.getenv("MAX_AGGREGATE_RISK_PERCENT", "6")),
        shadow_engine_enabled=_boolean("SHADOW_ENGINE_ENABLED", True),
        shadow_strategy=os.getenv("SHADOW_STRATEGY", "baseline_v1").strip(),
        shadow_decision_timeframe=os.getenv("SHADOW_DECISION_TIMEFRAME", "M5").upper(),
        shadow_min_rr=float(os.getenv("SHADOW_MIN_RR", "2")),
        shadow_max_spread_points=_nonnegative_int("SHADOW_MAX_SPREAD_POINTS", 100),
        shadow_notify_signals=_boolean("SHADOW_NOTIFY_SIGNALS", True),
        shadow_notify_no_trade=_boolean("SHADOW_NOTIFY_NO_TRADE", False),
        shadow_outcome_horizon_bars=_positive_int("SHADOW_OUTCOME_HORIZON_BARS", 12),
        forward_shadow_enabled=_boolean("FORWARD_SHADOW_ENABLED", False),
        forward_shadow_rr=float(os.getenv("FORWARD_SHADOW_RR", "2")),
        live_tick_interval_seconds=float(os.getenv("LIVE_TICK_INTERVAL_SECONDS", "1")),
        live_account_interval_seconds=float(os.getenv("LIVE_ACCOUNT_INTERVAL_SECONDS", "5")),
        live_candle_interval_seconds=float(os.getenv("LIVE_CANDLE_INTERVAL_SECONDS", "5")),
        live_history_interval_seconds=float(os.getenv("LIVE_HISTORY_INTERVAL_SECONDS", "30")),
        live_candle_lookback=_positive_int("LIVE_CANDLE_LOOKBACK", 100),
        mt5_reconnect_initial_seconds=float(os.getenv("MT5_RECONNECT_INITIAL_SECONDS", "1")),
        mt5_reconnect_max_seconds=float(os.getenv("MT5_RECONNECT_MAX_SECONDS", "30")),
        data_stale_tick_seconds=float(os.getenv("DATA_STALE_TICK_SECONDS", "10")),
        data_stale_account_seconds=float(os.getenv("DATA_STALE_ACCOUNT_SECONDS", "30")),
        data_stale_position_seconds=float(os.getenv("DATA_STALE_POSITION_SECONDS", "30")),
        websocket_tick_throttle_ms=_nonnegative_int("LIVE_WEBSOCKET_TICK_THROTTLE_MS", 500),
        telegram_control_enabled=_boolean("TELEGRAM_CONTROL_ENABLED"),
        telegram_allowed_chat_ids=_identifiers("TELEGRAM_ALLOWED_CHAT_IDS"),
        telegram_allowed_user_ids=_identifiers("TELEGRAM_ALLOWED_USER_IDS"),
        telegram_control_poll_seconds=float(os.getenv("TELEGRAM_CONTROL_POLL_SECONDS", "5")),
        telegram_control_rate_limit_seconds=float(
            os.getenv("TELEGRAM_CONTROL_RATE_LIMIT_SECONDS", "3")
        ),
        supervisor_max_restarts=_nonnegative_int("SUPERVISOR_MAX_RESTARTS", 3),
        supervisor_restart_window_seconds=float(
            os.getenv("SUPERVISOR_RESTART_WINDOW_SECONDS", "600")
        ),
        supervisor_operation_timeout_seconds=float(
            os.getenv("SUPERVISOR_OPERATION_TIMEOUT_SECONDS", "30")
        ),
    )
