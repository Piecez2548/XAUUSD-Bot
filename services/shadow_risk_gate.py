"""Independent deterministic risk gate for hypothetical shadow trades."""

from __future__ import annotations

from math import floor

from models.market import MarketSnapshot
from models.observatory import RiskSnapshot
from models.shadow import RiskGateResult


class ShadowRiskGate:
    def __init__(
        self,
        *,
        max_trade_risk_percent: float = 2.0,
        max_aggregate_risk_percent: float = 6.0,
    ) -> None:
        self.max_trade_risk_percent = min(max_trade_risk_percent, 2.0)
        self.max_aggregate_risk_percent = min(max_aggregate_risk_percent, 6.0)

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        risk: RiskSnapshot,
        *,
        entry: float,
        stop: float,
    ) -> tuple[RiskGateResult, float | None]:
        if risk.unbounded_positions_count > 0:
            return self._blocked("UNBOUNDED_EXISTING_POSITION")
        if risk.open_risk_percent is None or risk.remaining_risk_percent is None:
            return self._blocked("UNKNOWN_RISK")
        available = min(
            self.max_trade_risk_percent,
            risk.remaining_risk_percent,
            self.max_aggregate_risk_percent - risk.open_risk_percent,
        )
        if available <= 0:
            return self._blocked("RISK_BUDGET_EXCEEDED")
        tick_size = snapshot.symbol.trade_tick_size
        tick_value = snapshot.symbol.trade_tick_value_loss
        if tick_size <= 0 or tick_value <= 0 or snapshot.account.equity <= 0:
            return self._blocked("POSITION_SIZE_INVALID")
        risk_per_lot = abs(entry - stop) / tick_size * tick_value
        if risk_per_lot <= 0:
            return self._blocked("POSITION_SIZE_INVALID")
        raw_volume = snapshot.account.equity * available / 100 / risk_per_lot
        step = snapshot.symbol.volume_step
        volume = round(floor(raw_volume / step) * step, 10) if step > 0 else 0
        if volume < snapshot.symbol.volume_min or volume > snapshot.symbol.volume_max:
            return self._blocked("MIN_LOT_EXCEEDS_RISK_BUDGET")
        proposed = risk_per_lot * volume / snapshot.account.equity * 100
        if proposed > self.max_trade_risk_percent + 1e-9:
            return self._blocked("RISK_BUDGET_EXCEEDED")
        if risk.open_risk_percent + proposed > self.max_aggregate_risk_percent + 1e-9:
            return self._blocked("RISK_BUDGET_EXCEEDED")
        return (
            RiskGateResult(
                state="APPROVED",
                approved=True,
                reason_codes=("RISK_GATE_PASSED",),
                current_open_risk_percent=risk.open_risk_percent,
                remaining_aggregate_risk_percent=risk.remaining_risk_percent,
                proposed_risk_percent=proposed,
            ),
            volume,
        )

    @staticmethod
    def _blocked(reason: str) -> tuple[RiskGateResult, None]:
        return RiskGateResult(state="BLOCKED", approved=False, reason_codes=(reason,)), None
