"""Broker history facts; these models never issue MT5 commands."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DealFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    deal_ticket: int = Field(gt=0)
    order_ticket: int | None = Field(default=None, ge=0)
    position_id: int | None = Field(default=None, ge=0)
    symbol: str
    timestamp: datetime
    deal_type: str | None = None
    entry_type: str | None = None
    volume: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)
    profit: float | None = None
    commission: float | None = None
    swap: float | None = None
    fee: float | None = None
    comment: str | None = None
    magic_number: int | None = None
    reason: str | None = None
    raw_payload: dict[str, Any] | None = None

    @classmethod
    def from_mt5(cls, item: Any, *, symbol: str) -> DealFact:
        raw_time = int(getattr(item, "time", 0))
        if raw_time <= 0:
            raise ValueError("MT5 deal has no valid timestamp")
        return cls(
            deal_ticket=int(item.ticket),
            order_ticket=_optional_int(getattr(item, "order", None)),
            position_id=_optional_int(getattr(item, "position_id", None)),
            symbol=str(getattr(item, "symbol", symbol) or symbol),
            timestamp=datetime.fromtimestamp(raw_time, tz=UTC),
            deal_type=str(getattr(item, "type", "UNKNOWN")),
            entry_type=str(getattr(item, "entry", "UNKNOWN")),
            volume=_optional_float(getattr(item, "volume", None)),
            price=_optional_float(getattr(item, "price", None)),
            profit=_optional_float(getattr(item, "profit", None)),
            commission=_optional_float(getattr(item, "commission", None)),
            swap=_optional_float(getattr(item, "swap", None)),
            fee=_optional_float(getattr(item, "fee", None)),
            comment=str(getattr(item, "comment", "") or "") or None,
            magic_number=_optional_int(getattr(item, "magic", None)),
            reason=str(getattr(item, "reason", "") or "") or None,
            raw_payload=None,
        )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
