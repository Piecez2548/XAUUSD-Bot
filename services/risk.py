"""Read-only risk calculations based strictly on broker symbol specifications."""

from __future__ import annotations

from dataclasses import dataclass

from models.market import MarketSnapshot, Position
from models.observatory import PositionRisk, RiskSnapshot

RISK_PERCENT_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class NormalizedRiskState:
    """Stable economic risk state used for event/alert change detection."""

    aggregate_risk_percent: float | None
    remaining_budget_percent: float | None
    open_positions: int
    bounded_positions: int
    unbounded_positions: int
    aggregate_state: str
    hard_limit_state: str
    positions: tuple[tuple[int, bool, float | None], ...]


def normalize_risk_state(risk: RiskSnapshot) -> NormalizedRiskState:
    aggregate_state = (
        "UNBOUNDED"
        if risk.unbounded_positions_count
        else "LIMIT_EXCEEDED"
        if risk.open_risk_percent is not None
        and risk.open_risk_percent >= risk.max_aggregate_risk_percent
        else "WITHIN_LIMIT"
    )
    return NormalizedRiskState(
        aggregate_risk_percent=risk.open_risk_percent,
        remaining_budget_percent=risk.remaining_risk_percent,
        open_positions=risk.open_positions_count,
        bounded_positions=risk.open_positions_count - risk.unbounded_positions_count,
        unbounded_positions=risk.unbounded_positions_count,
        aggregate_state=aggregate_state,
        hard_limit_state=("EXCEEDED" if aggregate_state == "LIMIT_EXCEEDED" else "WITHIN_LIMIT"),
        positions=tuple(
            sorted(
                (
                    item.ticket,
                    item.bounded_by_stop,
                    item.risk_percent,
                )
                for item in risk.risk_per_position
            )
        ),
    )


def risk_state_changed(previous: NormalizedRiskState | None, current: RiskSnapshot) -> bool:
    """Compare only economically meaningful fields, ignoring timestamps/IDs."""

    if previous is None:
        return True
    candidate = normalize_risk_state(current)
    if (
        previous.open_positions != candidate.open_positions
        or previous.bounded_positions != candidate.bounded_positions
        or previous.unbounded_positions != candidate.unbounded_positions
        or previous.aggregate_state != candidate.aggregate_state
        or previous.hard_limit_state != candidate.hard_limit_state
        or not _positions_equal(previous.positions, candidate.positions)
    ):
        return True
    return not (
        _close_enough(previous.aggregate_risk_percent, candidate.aggregate_risk_percent)
        and _close_enough(previous.remaining_budget_percent, candidate.remaining_budget_percent)
    )


def _close_enough(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left == right
    return abs(left - right) < RISK_PERCENT_TOLERANCE


def _positions_equal(
    left: tuple[tuple[int, bool, float | None], ...],
    right: tuple[tuple[int, bool, float | None], ...],
) -> bool:
    if len(left) != len(right):
        return False
    return all(
        left_item[0] == right_item[0]
        and left_item[1] == right_item[1]
        and _close_enough(left_item[2], right_item[2])
        for left_item, right_item in zip(left, right, strict=True)
    )


def _position_risk_amount(snapshot: MarketSnapshot, position: Position) -> float | None:
    if position.stop_loss <= 0:
        return None
    # A stop only bounds loss when it is on the loss-producing side of the
    # entry.  Treat an invalid-side stop as unknown rather than understating
    # risk as a profitable/zero-risk position.
    if position.type not in {0, 1}:
        return None
    if position.type == 0 and position.stop_loss >= position.open_price:
        return None
    if position.type == 1 and position.stop_loss <= position.open_price:
        return None
    tick_size = snapshot.symbol.trade_tick_size
    tick_value = snapshot.symbol.trade_tick_value_loss
    if tick_size <= 0 or tick_value <= 0:
        return None
    distance = abs(position.open_price - position.stop_loss)
    return distance / tick_size * tick_value * position.volume


def calculate_risk_snapshot(
    snapshot: MarketSnapshot,
    *,
    max_trade_risk_percent: float,
    max_aggregate_risk_percent: float,
) -> RiskSnapshot:
    position_risks: list[PositionRisk] = []
    known_risk_amount = 0.0
    unbounded_positions = 0
    for position in snapshot.positions:
        amount = _position_risk_amount(snapshot, position)
        if amount is None:
            unbounded_positions += 1
            position_risks.append(PositionRisk(ticket=position.ticket, bounded_by_stop=False))
            continue
        percent = amount / snapshot.account.equity * 100 if snapshot.account.equity > 0 else None
        known_risk_amount += amount
        position_risks.append(
            PositionRisk(
                ticket=position.ticket,
                risk_amount=amount,
                risk_percent=percent,
                bounded_by_stop=True,
            )
        )

    risk_is_known = unbounded_positions == 0 and snapshot.account.equity > 0
    open_risk_amount = known_risk_amount if risk_is_known else None
    open_risk_percent = known_risk_amount / snapshot.account.equity * 100 if risk_is_known else None
    remaining = (
        max_aggregate_risk_percent - open_risk_percent if open_risk_percent is not None else None
    )
    margin_usage = (
        snapshot.account.margin / snapshot.account.equity * 100
        if snapshot.account.equity > 0
        else None
    )
    return RiskSnapshot(
        timestamp=snapshot.generated_at,
        equity=snapshot.account.equity,
        balance=snapshot.account.balance,
        open_risk_percent=open_risk_percent,
        open_risk_amount=open_risk_amount,
        remaining_risk_percent=remaining,
        risk_per_position=tuple(position_risks),
        max_trade_risk_percent=max_trade_risk_percent,
        max_aggregate_risk_percent=max_aggregate_risk_percent,
        open_positions_count=len(snapshot.positions),
        unbounded_positions_count=unbounded_positions,
        margin_usage_percent=margin_usage,
        free_margin=snapshot.account.free_margin,
    )
