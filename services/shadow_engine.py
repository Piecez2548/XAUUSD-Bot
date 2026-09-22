"""Deterministic, broker-independent Phase 2 shadow decision engine."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from models.market import MarketSnapshot, Timeframe
from models.observatory import RiskSnapshot
from models.shadow import (
    FeatureSet,
    MarketContext,
    MarketRegime,
    ShadowAction,
    ShadowDecision,
)
from services.features import FeatureValidationError, extract_features
from services.shadow_risk_gate import ShadowRiskGate


class ShadowDecisionEngine:
    """Pure decision logic. It accepts snapshots only and never imports MT5."""

    def __init__(
        self,
        *,
        target_risk_percent: float = 2.0,
        max_aggregate_risk_percent: float = 6.0,
        min_rr: float = 2.0,
        target_rr: float = 2.0,
        max_spread_points: int = 100,
        strategy_name: str = "baseline",
        strategy_version: str = "baseline_v1",
    ) -> None:
        self.target_risk_percent = target_risk_percent
        self.max_aggregate_risk_percent = max_aggregate_risk_percent
        self.min_rr = min_rr
        self.target_rr = target_rr
        self.max_spread_points = max_spread_points
        self.strategy_name = strategy_name
        self.strategy_version = strategy_version
        self.risk_gate = ShadowRiskGate(
            max_trade_risk_percent=target_risk_percent,
            max_aggregate_risk_percent=max_aggregate_risk_percent,
        )
        if not 0 < target_risk_percent <= 2:
            raise ValueError("target risk must be between zero and two percent")
        if max_aggregate_risk_percent <= 0 or min_rr <= 0 or target_rr <= 0:
            raise ValueError("aggregate risk and minimum RR must be positive")

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        *,
        market_snapshot_id: UUID | None = None,
        risk: RiskSnapshot | None = None,
        data_freshness: str = "LIVE",
        mt5_state: str = "CONNECTED",
        runtime_state: str = "CONNECTED",
        candles_are_closed: bool = False,
    ) -> ShadowDecision:
        m5_timestamp = self._m5_timestamp(snapshot, candles_are_closed=candles_are_closed)
        if data_freshness != "LIVE":
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                "DATA_STALE" if data_freshness == "STALE" else "DATA_INCOMPLETE",
                data_freshness,
            )
        if mt5_state != "CONNECTED":
            return self._no_trade(
                snapshot, market_snapshot_id, m5_timestamp, "MT5_NOT_CONNECTED", data_freshness
            )
        if runtime_state != "CONNECTED":
            return self._no_trade(
                snapshot, market_snapshot_id, m5_timestamp, "RUNTIME_NOT_CONNECTED", data_freshness
            )
        if risk is None:
            return self._no_trade(
                snapshot, market_snapshot_id, m5_timestamp, "RISK_STATE_UNKNOWN", data_freshness
            )
        try:
            features = self._extract(snapshot, candles_are_closed=candles_are_closed)
            context = self._context(features)
        except FeatureValidationError:
            return self._no_trade(
                snapshot, market_snapshot_id, m5_timestamp, "DATA_INCOMPLETE", data_freshness
            )

        feature_context = {name: item.model_dump(mode="json") for name, item in features.items()}
        if context.regime in {MarketRegime.HIGH_VOLATILITY, MarketRegime.LOW_VOLATILITY}:
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                "VOLATILITY_FILTER",
                data_freshness,
                regime=context.regime,
                context=context,
                feature_context=feature_context,
            )
        direction = self._strategy_direction(context)
        if direction is None:
            reason = (
                "MARKET_REGIME_UNCERTAIN"
                if context.regime == MarketRegime.UNCERTAIN
                else "TREND_NOT_ALIGNED"
            )
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                reason,
                data_freshness,
                regime=context.regime,
                context=context,
                feature_context=feature_context,
            )
        if snapshot.symbol.spread > self.max_spread_points:
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                "SPREAD_TOO_HIGH",
                data_freshness,
                regime=context.regime,
                context=context,
                feature_context=feature_context,
            )

        entry = snapshot.symbol.ask if direction == ShadowAction.BUY else snapshot.symbol.bid
        m5 = features[Timeframe.M5.value]
        atr = m5.atr or m5.true_range
        if direction == ShadowAction.BUY:
            stop = (m5.swing_low or 0) - atr * 0.25
            distance = entry - stop
        else:
            stop = (m5.swing_high or 0) + atr * 0.25
            distance = stop - entry
        if stop <= 0 or distance <= snapshot.symbol.trade_tick_size:
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                "INVALID_SL",
                data_freshness,
                regime=context.regime,
                context=context,
                feature_context=feature_context,
            )
        target = (
            entry + distance * self.target_rr
            if direction == ShadowAction.BUY
            else entry - distance * self.target_rr
        )
        rr = abs(target - entry) / distance
        if rr < self.min_rr:
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                "RR_TOO_LOW",
                data_freshness,
                regime=context.regime,
                context=context,
                feature_context=feature_context,
            )

        gate, volume = self._risk_gate(snapshot, risk, entry, stop)
        if not gate.approved or volume is None:
            return self._no_trade(
                snapshot,
                market_snapshot_id,
                m5_timestamp,
                gate.reason_codes[0] if gate.reason_codes else "RISK_GATE_BLOCKED",
                data_freshness,
                regime=context.regime,
                context=context,
                feature_context=feature_context,
                risk_gate=gate,
            )
        confidence = (
            0.75 if context.m15_setup == direction and context.m5_timing == direction else 0.6
        )
        return ShadowDecision(
            market_snapshot_id=market_snapshot_id,
            symbol=snapshot.symbol.name,
            m5_candle_timestamp=m5.candle_timestamp,
            decision=direction,
            market_regime=context.regime,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            risk_reward_ratio=rr,
            requested_risk_percent=self.target_risk_percent,
            approved_risk_percent=gate.proposed_risk_percent,
            hypothetical_volume=volume,
            confidence=confidence,
            strategy_name=self.strategy_name,
            strategy_version=self.strategy_version,
            reason_codes=("VALID_SETUP",),
            human_readable_reason=(
                "Multi-timeframe directional alignment passed the deterministic risk gate."
            ),
            risk_gate_state=gate.state,
            data_freshness=data_freshness,
            feature_context=feature_context,
        )

    def _extract(
        self, snapshot: MarketSnapshot, *, candles_are_closed: bool = False
    ) -> dict[str, FeatureSet]:
        required = (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5)
        missing = [timeframe.value for timeframe in required if not snapshot.candles.get(timeframe)]
        if missing:
            raise FeatureValidationError(f"missing timeframe: {','.join(missing)}")
        return {
            timeframe.value: extract_features(
                snapshot.candles[timeframe],
                timeframe=timeframe.value,
                spread=snapshot.symbol.spread,
                forming_last=not candles_are_closed,
            )
            for timeframe in required
        }

    @staticmethod
    def _context(features: Mapping[str, FeatureSet]) -> MarketContext:
        def bias(item: FeatureSet) -> ShadowAction:
            if item.price_vs_ema is not None and item.ema_slope is not None:
                if (
                    item.price_vs_ema > 0
                    and item.ema_slope >= 0
                    and item.higher_high
                    and item.higher_low
                ):
                    return ShadowAction.BUY
                if (
                    item.price_vs_ema < 0
                    and item.ema_slope <= 0
                    and item.lower_high
                    and item.lower_low
                ):
                    return ShadowAction.SELL
            return ShadowAction.NO_TRADE

        h4, h1, m15, m5 = (features[key] for key in ("H4", "H1", "M15", "M5"))
        h4_bias, h1_bias = bias(h4), bias(h1)
        m15_bias, m5_bias = bias(m15), bias(m5)
        relative = m5.relative_volatility
        if relative is not None and relative >= 2.0:
            regime = MarketRegime.HIGH_VOLATILITY
        elif relative is not None and relative <= 0.5:
            regime = MarketRegime.LOW_VOLATILITY
        elif h4_bias == h1_bias == ShadowAction.BUY:
            regime = MarketRegime.TREND_UP
        elif h4_bias == h1_bias == ShadowAction.SELL:
            regime = MarketRegime.TREND_DOWN
        elif h4_bias == h1_bias == ShadowAction.NO_TRADE:
            regime = MarketRegime.RANGE
        else:
            regime = MarketRegime.UNCERTAIN
        return MarketContext(
            regime=regime,
            h4_bias=h4_bias,
            h1_bias=h1_bias,
            m15_setup=m15_bias,
            m5_timing=m5_bias,
            features=dict(features),
            reason_codes=(
                "TREND_ALIGNMENT"
                if h4_bias == h1_bias != ShadowAction.NO_TRADE
                else "CONFLICTING_CONTEXT",
            ),
        )

    @staticmethod
    def _strategy_direction(context: MarketContext) -> ShadowAction | None:
        if context.h4_bias != context.h1_bias or context.h4_bias == ShadowAction.NO_TRADE:
            return None
        if context.m15_setup != context.h4_bias or context.m5_timing != context.h4_bias:
            return None
        return context.h4_bias

    def _risk_gate(
        self,
        snapshot: MarketSnapshot,
        risk: RiskSnapshot,
        entry: float,
        stop: float,
    ) -> tuple[Any, float | None]:
        return self.risk_gate.evaluate(snapshot, risk, entry=entry, stop=stop)

    @staticmethod
    def _m5_timestamp(snapshot: MarketSnapshot, *, candles_are_closed: bool = False) -> datetime:
        values = snapshot.candles.get(Timeframe.M5, ())
        return (
            values[-1].timestamp
            if candles_are_closed and values
            else values[-2].timestamp
            if len(values) > 1
            else (values[-1].timestamp if values else datetime.now(UTC))
        )

    def _no_trade(
        self,
        snapshot: MarketSnapshot,
        market_snapshot_id: UUID | None,
        m5_timestamp: datetime,
        reason: str,
        data_freshness: str,
        *,
        regime: MarketRegime = MarketRegime.UNCERTAIN,
        context: MarketContext | None = None,
        feature_context: dict[str, object] | None = None,
        risk_gate: Any | None = None,
    ) -> ShadowDecision:
        return ShadowDecision(
            market_snapshot_id=market_snapshot_id,
            symbol=snapshot.symbol.name,
            m5_candle_timestamp=m5_timestamp,
            decision=ShadowAction.NO_TRADE,
            market_regime=regime,
            strategy_name=self.strategy_name,
            strategy_version=self.strategy_version,
            reason_codes=(reason,),
            human_readable_reason=reason.replace("_", " ").title(),
            risk_gate_state=risk_gate.state if risk_gate is not None else "NOT_EVALUATED",
            data_freshness=data_freshness,
            feature_context=feature_context or {},
        )
