"""Read-only account queries."""

from __future__ import annotations

from typing import Any

from models.market import AccountState


class AccountReadError(RuntimeError):
    """Raised when account state cannot be read."""


ACCOUNT_TRADE_MODES = {
    0: "demo",
    1: "contest",
    2: "real",
}


def read_account_state(api: Any) -> AccountState:
    info = api.account_info()
    if info is None:
        raise AccountReadError(f"MT5 account_info failed: {api.last_error()!r}")

    trade_mode = getattr(info, "trade_mode", None)
    return AccountState(
        balance=float(info.balance),
        equity=float(info.equity),
        margin=float(info.margin),
        free_margin=float(info.margin_free),
        margin_level=float(info.margin_level),
        profit=float(info.profit),
        leverage=int(info.leverage),
        currency=str(info.currency),
        server=str(info.server),
        trade_mode=int(trade_mode) if trade_mode is not None else None,
        trade_mode_name=ACCOUNT_TRADE_MODES.get(int(trade_mode), "unknown")
        if trade_mode is not None
        else None,
    )
