"""Deterministic M15-context/M5-breakout executable setup.

This strategy is independent from Pair Zone.  It only consumes closed candles
and produces a normal ``ShadowDecision``; execution remains owned by the
existing Forward Shadow -> DemoExecutionService safety path.
"""

from __future__ import annotations

import hashlib
import json
from math import isfinite
from typing import Any

from models.market import Candle, MarketSnapshot, Timeframe
from models.shadow import ShadowAction, ShadowDecision
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_risk_gate import ShadowRiskGate
from services.strategy_platform import StrategyMetadata, config_hash

MOMENTUM_SETUP_TYPE = "MOMENTUM_BREAKOUT_V1"
MOMENTUM_CONFIG = {
    "strategy_id": "momentum_breakout_v1",
    "strategy_version": "1.0.0",
    "config_version": "momentum_breakout_v1_frozen",
    "m15_ema_period": 20,
    "m5_atr_period": 14,
    "swing_left_bars": 2,
    "swing_right_bars": 2,
    "minimum_body_atr": 0.5,
    "minimum_body_ratio": 0.6,
    "target_rr": 2.0,
    "stop_buffer_ticks": 1,
    "max_spread_points": 300,
}


def _ema(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
    return current


def _true_range(current: Candle, previous_close: float) -> float:
    return max(
        current.high - current.low,
        abs(current.high - previous_close),
        abs(current.low - previous_close),
    )


def _candle_json(candle: Candle) -> dict[str, Any]:
    return {
        "timestamp": candle.timestamp.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "tick_volume": candle.tick_volume,
        "spread": candle.spread,
        "real_volume": candle.real_volume,
    }


def _normalize_price(value: float, tick_size: float, digits: int) -> float:
    return round(round(value / tick_size) * tick_size, digits)


class MomentumBreakoutV1:
    """Small, causal breakout hypothesis with explicit rejection reasons."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.config = dict(MOMENTUM_CONFIG)
        self.metadata = StrategyMetadata(
            strategy_id=MOMENTUM_CONFIG["strategy_id"],
            strategy_version=MOMENTUM_CONFIG["strategy_version"],
            config_version=MOMENTUM_CONFIG["config_version"],
            config_hash=config_hash(MOMENTUM_CONFIG),
            display_name="Momentum Breakout V1",
        )
        self.validate_config()

    @property
    def strategy_version(self) -> str:
        return self.metadata.strategy_version

    def required_timeframes(self) -> tuple[Timeframe, ...]:
        return (Timeframe.M15, Timeframe.M5)

    def validate_config(self) -> None:
        if self.config["m15_ema_period"] <= 1 or self.config["m5_atr_period"] <= 1:
            raise ValueError("momentum periods must be greater than one")
        if (
            self.config["target_rr"] <= 0
            or self.config["stop_buffer_ticks"] < 0
            or self.config["max_spread_points"] < 0
        ):
            raise ValueError("momentum risk geometry is invalid")
        if not 0 < self.config["minimum_body_ratio"] <= 1:
            raise ValueError("momentum body ratio is invalid")

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        *,
        market_snapshot_id=None,
        risk=None,
        data_freshness: str = "LIVE",
        mt5_state: str = "CONNECTED",
        runtime_state: str = "CONNECTED",
        candles_are_closed: bool = False,
    ) -> ShadowDecision:
        m5 = snapshot.candles.get(Timeframe.M5, ())
        timestamp = ShadowDecisionEngine._m5_timestamp(
            snapshot, candles_are_closed=candles_are_closed
        )
        common = {
            "market_snapshot_id": market_snapshot_id,
            "symbol": snapshot.symbol.name,
            "m5_candle_timestamp": timestamp,
            "strategy_name": MOMENTUM_SETUP_TYPE,
            "strategy_version": self.metadata.strategy_version,
            "config_version": self.metadata.config_version,
            "config_hash": self.metadata.config_hash,
            "data_freshness": data_freshness,
            "execution_allowed": False,
        }
        if not candles_are_closed:
            return self._no_trade(common, "CANDLE_INCOMPLETE", "DATA")
        if data_freshness != "LIVE":
            return self._no_trade(common, "DATA_STALE", "DATA")
        if mt5_state != "CONNECTED" or runtime_state != "CONNECTED":
            return self._no_trade(common, "RUNTIME_NOT_CONNECTED", "CONTEXT")
        if risk is None:
            return self._no_trade(common, "RISK_STATE_UNKNOWN", "RISK_GATE")
        if not self._ordered_closed(m5):
            return self._no_trade(common, "CANDLE_ORDER_INVALID", "DATA")
        if snapshot.symbol.spread > self.config["max_spread_points"]:
            return self._no_trade(common, "SPREAD_TOO_HIGH", "SPREAD")
        m15 = snapshot.candles.get(Timeframe.M15, ())
        if not self._ordered_closed(m15) or any(
            candle.timestamp > timestamp for candle in m15
        ):
            return self._no_trade(common, "M15_CONTEXT_INVALID", "M15_CONTEXT")
        try:
            direction, m15_context = self._m15_direction(m15)
        except (KeyError, ValueError):
            return self._no_trade(common, "M15_CONTEXT_INVALID", "M15_CONTEXT")
        if direction is None:
            return self._no_trade(common, "M15_DIRECTION_MISSING", "M15_CONTEXT")
        if len(m5) < self.config["m5_atr_period"] + 8:
            return self._no_trade(common, "M5_STRUCTURE_INSUFFICIENT", "M5_STRUCTURE")

        trigger = m5[-1]
        prior = m5[:-1]
        swing_high = self._latest_swing(prior, high=True)
        swing_low = self._latest_swing(prior, high=False)
        if swing_high is None or swing_low is None:
            return self._no_trade(common, "M5_STRUCTURE_MISSING", "M5_STRUCTURE")
        atr = self._atr(prior[-(self.config["m5_atr_period"] + 1) :])
        if atr <= 0:
            return self._no_trade(common, "M5_ATR_INVALID", "DISPLACEMENT")
        body = abs(trigger.close - trigger.open)
        range_width = trigger.high - trigger.low
        displacement = (
            body >= atr * self.config["minimum_body_atr"]
            and body >= snapshot.symbol.trade_tick_size
        )
        body_dominant = range_width > 0 and body / range_width >= self.config["minimum_body_ratio"]
        if direction is ShadowAction.BUY:
            breakout_level = swing_high[1].high
            continuation = self._continuation(prior[-3:], bullish=True)
            broke = trigger.close > breakout_level + snapshot.symbol.trade_tick_size
            bullish_candle = trigger.close > trigger.open
            structural = swing_low
            entry = snapshot.tick.ask
        else:
            breakout_level = swing_low[1].low
            continuation = self._continuation(prior[-3:], bullish=False)
            broke = trigger.close < breakout_level - snapshot.symbol.trade_tick_size
            bullish_candle = trigger.close < trigger.open
            structural = swing_high
            entry = snapshot.tick.bid
        if not continuation:
            return self._no_trade(common, "M5_CONTINUATION_MISSING", "M5_STRUCTURE")
        if not broke:
            return self._no_trade(common, "BREAKOUT_CLOSE_MISSING", "BREAKOUT")
        if not bullish_candle or not displacement or not body_dominant:
            return self._no_trade(common, "DISPLACEMENT_INSUFFICIENT", "DISPLACEMENT")
        if not isfinite(entry) or entry <= 0:
            return self._no_trade(common, "EXECUTABLE_PRICE_INVALID", "PRICE")
        buffer = snapshot.symbol.trade_tick_size * self.config["stop_buffer_ticks"]
        stop = (
            structural[1].low - buffer
            if direction is ShadowAction.BUY
            else structural[1].high + buffer
        )
        stop = _normalize_price(stop, snapshot.symbol.trade_tick_size, snapshot.symbol.digits)
        entry = _normalize_price(entry, snapshot.symbol.trade_tick_size, snapshot.symbol.digits)
        distance = entry - stop if direction is ShadowAction.BUY else stop - entry
        if distance <= snapshot.symbol.trade_tick_size:
            return self._no_trade(common, "STRUCTURAL_SL_INVALID", "GEOMETRY")
        target = _normalize_price(
            entry + distance * self.config["target_rr"]
            if direction is ShadowAction.BUY
            else entry - distance * self.config["target_rr"],
            snapshot.symbol.trade_tick_size,
            snapshot.symbol.digits,
        )
        gate, volume = ShadowRiskGate(
            max_trade_risk_percent=self.settings.max_trade_risk_percent,
            max_aggregate_risk_percent=self.settings.max_aggregate_risk_percent,
        ).evaluate(snapshot, risk, entry=entry, stop=stop)
        if not gate.approved or volume is None:
            return self._no_trade(common, gate.reason_codes[0], "RISK_GATE")
        structure_candle = swing_high[1] if direction is ShadowAction.BUY else swing_low[1]
        setup_event_id = self._setup_event_id(
            snapshot.symbol.name, direction.value, structure_candle, breakout_level
        )
        context = {
            "setup_type": MOMENTUM_SETUP_TYPE,
            "setup_event_id": setup_event_id,
            "m15_context": m15_context,
            "m5_trigger_candle": _candle_json(trigger),
            "breakout_level": breakout_level,
            "structural_sl_source": _candle_json(structural[1]),
            "atr": atr,
            "displacement_body": body,
            "spread_points": snapshot.symbol.spread,
        }
        return ShadowDecision(
            **common,
            decision=direction,
            market_regime="TREND_UP" if direction is ShadowAction.BUY else "TREND_DOWN",
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            risk_reward_ratio=self.config["target_rr"],
            requested_risk_percent=min(self.settings.max_trade_risk_percent, 2.0),
            approved_risk_percent=gate.proposed_risk_percent,
            hypothetical_volume=volume,
            confidence=0.6,
            reason_codes=("VALID_MOMENTUM_BREAKOUT",),
            human_readable_reason=(
                "Closed M5 displacement broke a confirmed swing in the M15 direction."
            ),
            risk_gate_state=gate.state,
            feature_context=context,
        )

    def _m15_direction(
        self, candles: tuple[Candle, ...]
    ) -> tuple[ShadowAction | None, dict[str, Any]]:
        period = self.config["m15_ema_period"]
        if len(candles) < period + 1:
            raise ValueError("insufficient M15 context")
        closes = [c.close for c in candles]
        ema = _ema(closes[-period:], period)
        prior_ema = _ema(closes[-period - 1 : -1], period)
        latest = candles[-1]
        previous = candles[-2]
        context = {
            "timestamp": latest.timestamp.isoformat(),
            "close": latest.close,
            "ema": ema,
            "prior_ema": prior_ema,
            "previous_close": previous.close,
        }
        if latest.close > ema and ema > prior_ema and latest.close > previous.close:
            return ShadowAction.BUY, context
        if latest.close < ema and ema < prior_ema and latest.close < previous.close:
            return ShadowAction.SELL, context
        return None, context

    @staticmethod
    def _ordered_closed(candles: tuple[Candle, ...]) -> bool:
        return bool(candles) and all(
            right.timestamp > left.timestamp
            for left, right in zip(candles, candles[1:], strict=False)
        )

    @staticmethod
    def _latest_swing(candles: tuple[Candle, ...], *, high: bool) -> tuple[int, Candle] | None:
        left_right = 2
        candidates: list[tuple[int, Candle]] = []
        for index in range(left_right, len(candles) - left_right):
            pivot = candles[index]
            values = (
                [c.high for c in candles[index - left_right : index]]
                + [c.high for c in candles[index + 1 : index + left_right + 1]]
                if high
                else [c.low for c in candles[index - left_right : index]]
                + [c.low for c in candles[index + 1 : index + left_right + 1]]
            )
            pivot_value = pivot.high if high else pivot.low
            if (pivot_value > max(values)) if high else (pivot_value < min(values)):
                candidates.append((index, pivot))
        return candidates[-1] if candidates else None

    @staticmethod
    def _continuation(candles: tuple[Candle, ...], *, bullish: bool) -> bool:
        if len(candles) != 3:
            return False
        closes = [c.close for c in candles]
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        if bullish:
            return closes[0] < closes[1] < closes[2] and lows[0] <= lows[1] <= lows[2]
        return closes[0] > closes[1] > closes[2] and highs[0] >= highs[1] >= highs[2]

    @staticmethod
    def _atr(candles: tuple[Candle, ...]) -> float:
        if len(candles) < 2:
            return 0.0
        ranges = [
            _true_range(candle, previous.close)
            for previous, candle in zip(candles, candles[1:], strict=False)
        ]
        return sum(ranges) / len(ranges) if ranges else 0.0

    def _setup_event_id(self, symbol: str, direction: str, trigger: Candle, level: float) -> str:
        material = json.dumps(
            {
                "setup_type": MOMENTUM_SETUP_TYPE,
                "symbol": symbol,
                "direction": direction,
                "trigger": trigger.timestamp.isoformat(),
                "breakout_level": level,
                "config_hash": self.metadata.config_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "momentum-event-" + hashlib.sha256(material.encode()).hexdigest()[:32]

    @staticmethod
    def _no_trade(common: dict[str, Any], reason: str, stage: str) -> ShadowDecision:
        return ShadowDecision(
            **common,
            decision=ShadowAction.NO_TRADE,
            market_regime="UNCERTAIN",
            reason_codes=(reason,),
            human_readable_reason=reason.replace("_", " ").title(),
            risk_gate_state="NOT_EVALUATED",
            feature_context={"setup_type": MOMENTUM_SETUP_TYPE, "rejection_stage": stage},
        )

    def explain(self) -> str:
        return (
            "M15 EMA20 direction plus closed M5 confirmed-swing displacement "
            "breakout with structural SL and 2R TP."
        )
