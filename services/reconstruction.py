"""Conservative, read-only reconstruction of completed broker positions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from models.history import DealFact


@dataclass(frozen=True, slots=True)
class ReconstructedTrade:
    position_id: int
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float | None
    exit_price: float | None
    volume: float | None
    net_profit: float | None
    deal_tickets: tuple[int, ...]


def reconstruct_closed_positions(facts: tuple[DealFact, ...]) -> tuple[ReconstructedTrade, ...]:
    """Return only unambiguous positions with both entry and exit deal facts.

    No inferred close, direction, stop, or reason is produced. Partial fills are
    retained as a single aggregate only when all facts share a broker position id.
    """

    groups: dict[int, list[DealFact]] = {}
    for fact in facts:
        if fact.position_id is not None:
            groups.setdefault(fact.position_id, []).append(fact)
    result: list[ReconstructedTrade] = []
    for position_id, items in groups.items():
        ordered = sorted(items, key=lambda item: (item.timestamp, item.deal_ticket))
        entries = [item for item in ordered if _is_entry(item.entry_type)]
        exits = [item for item in ordered if _is_exit(item.entry_type)]
        if not entries or not exits:
            continue
        entry = entries[0]
        exit_fact = exits[-1]
        result.append(
            ReconstructedTrade(
                position_id=position_id,
                symbol=entry.symbol,
                entry_time=entry.timestamp.astimezone(UTC),
                exit_time=exit_fact.timestamp.astimezone(UTC),
                entry_price=entry.price,
                exit_price=exit_fact.price,
                volume=sum(item.volume or 0.0 for item in entries) or None,
                net_profit=sum(item.profit or 0.0 for item in ordered),
                deal_tickets=tuple(item.deal_ticket for item in ordered),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.exit_time, item.position_id)))


def _is_entry(value: str | None) -> bool:
    return value is not None and value.upper() in {"0", "IN", "ENTRY", "DEAL_ENTRY_IN"}


def _is_exit(value: str | None) -> bool:
    return value is not None and value.upper() in {"1", "OUT", "EXIT", "DEAL_ENTRY_OUT"}
