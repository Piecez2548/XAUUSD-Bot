"""Read-only MT5 deal history adapter with conservative mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from models.history import DealFact


class HistoryReadError(RuntimeError):
    """Raised when MT5 history cannot be queried."""


def read_deals(api: Any, symbol: str, start: datetime, end: datetime) -> tuple[DealFact, ...]:
    history_get = getattr(api, "history_deals_get", None)
    if history_get is None:
        raise HistoryReadError("MT5 history_deals_get is unavailable")
    result = history_get(start.astimezone(UTC), end.astimezone(UTC), group=symbol)
    if result is None:
        raise HistoryReadError(f"MT5 history_deals_get failed for {symbol}: {api.last_error()!r}")
    facts: list[DealFact] = []
    for item in result:
        fact = DealFact.from_mt5(item, symbol=symbol)
        if fact.symbol == symbol:
            facts.append(fact)
    return tuple(sorted(facts, key=lambda item: (item.timestamp, item.deal_ticket)))


def history_window(
    cursor: datetime | None, *, overlap_seconds: int = 5
) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    start = cursor - timedelta(seconds=overlap_seconds) if cursor else end - timedelta(days=1)
    return start, end
