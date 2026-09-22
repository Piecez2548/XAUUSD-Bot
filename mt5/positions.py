"""Read-only open position queries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from models.market import Position


class PositionReadError(RuntimeError):
    """Raised when MT5 cannot return open positions."""


POSITION_TYPES = {
    0: "BUY",
    1: "SELL",
}


def read_open_positions(api: Any, symbol: str) -> tuple[Position, ...]:
    raw_positions = api.positions_get(symbol=symbol)
    if raw_positions is None:
        raise PositionReadError(f"MT5 positions_get failed for {symbol}: {api.last_error()!r}")

    positions: list[Position] = []
    for item in raw_positions:
        raw_time = int(item.time)
        position_type = int(item.type)
        positions.append(
            Position(
                ticket=int(item.ticket),
                symbol=str(item.symbol),
                type=position_type,
                type_name=POSITION_TYPES.get(position_type, f"UNKNOWN_{position_type}"),
                volume=float(item.volume),
                open_price=float(item.price_open),
                current_price=float(item.price_current),
                stop_loss=float(item.sl),
                take_profit=float(item.tp),
                profit=float(item.profit),
                swap=float(item.swap),
                magic_number=int(item.magic),
                comment=str(item.comment),
                open_time=datetime.fromtimestamp(raw_time, tz=UTC),
                raw_open_time=raw_time,
            )
        )
    return tuple(positions)
