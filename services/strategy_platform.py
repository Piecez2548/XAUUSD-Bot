"""Versioned, replaceable read-only strategy plugins for shadow research."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from models.market import MarketSnapshot, Timeframe
from models.observatory import RiskSnapshot
from models.shadow import ShadowAction, ShadowDecision
from services.features import FeatureValidationError, extract_features
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_risk_gate import ShadowRiskGate


@dataclass(frozen=True, slots=True)
class StrategyMetadata:
    strategy_id: str
    strategy_version: str
    config_version: str
    config_hash: str
    display_name: str


@dataclass(frozen=True, slots=True)
class StrategyDecisionCandidate:
    """Normalized strategy intent; execution is structurally impossible."""

    metadata: StrategyMetadata
    decision: ShadowDecision
    setup_type: str | None = None
    rejection_stage: str | None = None
    execution_allowed: bool = False


class Strategy(Protocol):
    metadata: StrategyMetadata

    def required_timeframes(self) -> tuple[Timeframe, ...]: ...
    def validate_config(self) -> None: ...
    def evaluate(self, snapshot: MarketSnapshot, *, market_snapshot_id=None,
                 risk: RiskSnapshot | None = None, data_freshness: str = "LIVE",
                 mt5_state: str = "CONNECTED", runtime_state: str = "CONNECTED",
                 candles_are_closed: bool = False) -> ShadowDecision: ...
    def explain(self) -> str: ...


def config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_config(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class BaselineV1Adapter:
    metadata = StrategyMetadata(
        strategy_id="baseline", strategy_version="baseline_v1",
        config_version="baseline_v1_frozen", config_hash=config_hash({
            "strategy": "baseline_v1", "rules": "phase-2.0-frozen"
        }), display_name="Baseline V1",
    )

    def __init__(self, settings) -> None:
        self.engine = ShadowDecisionEngine(
            target_risk_percent=min(settings.max_trade_risk_percent, 2.0),
            max_aggregate_risk_percent=min(settings.max_aggregate_risk_percent, 6.0),
            min_rr=settings.shadow_min_rr,
            max_spread_points=settings.shadow_max_spread_points,
            strategy_name="baseline", strategy_version="baseline_v1",
        )

    @property
    def strategy_version(self) -> str:
        return self.metadata.strategy_version

    def required_timeframes(self) -> tuple[Timeframe, ...]:
        return (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5)

    def validate_config(self) -> None:
        return None

    def evaluate(self, snapshot, **kwargs):
        decision = self.engine.evaluate(snapshot, **kwargs)
        return decision.model_copy(
            update={
                "config_version": self.metadata.config_version,
                "config_hash": self.metadata.config_hash,
            }
        )

    def explain(self) -> str:
        return "Frozen Phase 2 baseline adapter; rules are not modified by the plugin platform."


class TrendPullbackV1:
    def __init__(self, settings, *, config_path: Path | None = None) -> None:
        path = config_path or Path(__file__).resolve().parents[1] / "config" / "strategies" / "trend_pullback_v1.yaml"
        self.config = _load_config(path)
        self.metadata = StrategyMetadata(
            strategy_id=str(self.config["strategy_id"]),
            strategy_version=str(self.config["strategy_version"]),
            config_version=str(self.config["config_version"]),
            config_hash=config_hash(self.config),
            display_name="Trend Pullback V1 (Research)",
        )
        self.target_rr = float(self.config["target_rr"])
        self.max_spread_points = int(self.config["max_spread_points"])
        self.stop_buffer = float(self.config["atr_stop_buffer"])
        self.validate_config()

    @property
    def strategy_version(self) -> str:
        return self.metadata.strategy_version

    def required_timeframes(self) -> tuple[Timeframe, ...]:
        return (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5)

    def validate_config(self) -> None:
        if self.target_rr <= 0 or self.stop_buffer < 0:
            raise ValueError("trend_pullback configuration is invalid")
        if int(self.config["ema_fast_period"]) >= int(self.config["ema_slow_period"]):
            raise ValueError("fast EMA must be shorter than slow EMA")

    def evaluate(self, snapshot, *, market_snapshot_id=None, risk=None,
                 data_freshness="LIVE", mt5_state="CONNECTED",
                 runtime_state="CONNECTED", candles_are_closed=False):
        timestamp = ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=candles_are_closed)
        common = dict(market_snapshot_id=market_snapshot_id, symbol=snapshot.symbol.name,
                      m5_candle_timestamp=timestamp, strategy_name="trend_pullback",
                      strategy_version=self.metadata.strategy_version,
                      config_version=self.metadata.config_version, config_hash=self.metadata.config_hash,
                      data_freshness=data_freshness, execution_allowed=False)
        if data_freshness != "LIVE":
            return self._no_trade(common, "DATA_STALE", "DATA")
        if mt5_state != "CONNECTED" or runtime_state != "CONNECTED":
            return self._no_trade(common, "RUNTIME_NOT_CONNECTED", "CONTEXT")
        if risk is None:
            return self._no_trade(common, "RISK_STATE_UNKNOWN", "RISK_GATE")
        try:
            features = {tf.value: extract_features(snapshot.candles[tf], timeframe=tf.value,
                                                    spread=snapshot.symbol.spread,
                                                    forming_last=not candles_are_closed,
                                                    atr_period=int(self.config["atr_period"]))
                        for tf in self.required_timeframes()}
        except (FeatureValidationError, KeyError):
            return self._no_trade(common, "DATA_INCOMPLETE", "DATA")
        direction = self._htf_direction(snapshot)
        if direction is None:
            return self._no_trade(common, "HTF_TREND_NOT_ALIGNED", "HTF_TREND")
        m15 = features["M15"]
        atr = m15.atr or m15.true_range
        ema = m15.ema or m15.close
        pullback_distance = abs(m15.close - ema)
        if pullback_distance > atr * float(self.config["pullback_atr_tolerance"]):
            return self._no_trade(common, "M15_PULLBACK_NOT_DETECTED", "M15_PULLBACK")
        m5 = features["M5"]
        confirming = (m5.close >= m5.open and m5.close > ema) if direction == ShadowAction.BUY else (m5.close <= m5.open and m5.close < ema)
        if not confirming:
            return self._no_trade(common, "M5_CONFIRMATION_MISSING", "M5_CONFIRMATION")
        if snapshot.symbol.spread > self.max_spread_points:
            return self._no_trade(common, "SPREAD_TOO_HIGH", "SPREAD")
        entry = m5.close
        stop = (m5.swing_low or entry) - atr * self.stop_buffer if direction == ShadowAction.BUY else (m5.swing_high or entry) + atr * self.stop_buffer
        distance = entry - stop if direction == ShadowAction.BUY else stop - entry
        if distance <= snapshot.symbol.trade_tick_size:
            return self._no_trade(common, "INVALID_SL", "GEOMETRY")
        target = entry + distance * self.target_rr if direction == ShadowAction.BUY else entry - distance * self.target_rr
        gate, volume = ShadowRiskGate(max_trade_risk_percent=2, max_aggregate_risk_percent=6).evaluate(snapshot, risk, entry=entry, stop=stop)
        if not gate.approved or volume is None:
            return self._no_trade(common, gate.reason_codes[0], "RISK_GATE")
        return ShadowDecision(**common, decision=direction, market_regime="TREND_UP" if direction == ShadowAction.BUY else "TREND_DOWN",
                              entry_price=entry, stop_loss=stop, take_profit=target,
                              risk_reward_ratio=self.target_rr, requested_risk_percent=2.0,
                              approved_risk_percent=gate.proposed_risk_percent,
                              hypothetical_volume=volume, confidence=0.6,
                              reason_codes=("VALID_TREND_PULLBACK",),
                              human_readable_reason="Initial research hypothesis: HTF trend, M15 pullback and M5 confirmation.",
                              risk_gate_state=gate.state, feature_context={name: item.model_dump(mode="json") for name, item in features.items()})

    def _htf_direction(self, snapshot):
        values = []
        for tf in (Timeframe.H4, Timeframe.H1):
            candles = snapshot.candles.get(tf, ())
            if len(candles) < 50:
                return None
            closes = [c.close for c in candles[:-1] if c.close > 0]
            fast = _ema(closes, 20)
            slow = _ema(closes, 50)
            slope = fast - _ema(closes[:-1], 20)
            values.append(ShadowAction.BUY if fast > slow and slope > 0 and closes[-1] > fast else ShadowAction.SELL if fast < slow and slope < 0 and closes[-1] < fast else ShadowAction.NO_TRADE)
        return values[0] if values[0] == values[1] and values[0] != ShadowAction.NO_TRADE else None

    @staticmethod
    def _no_trade(common, reason, stage):
        return ShadowDecision(**common, decision=ShadowAction.NO_TRADE, market_regime="UNCERTAIN",
                              reason_codes=(reason,), human_readable_reason=reason.replace("_", " ").title(),
                              risk_gate_state="NOT_EVALUATED", feature_context={"rejection_stage": stage})

    def explain(self) -> str:
        return "Initial research hypothesis: H4/H1 EMA20/EMA50 trend, M15 ATR-normalized pullback, M5 close confirmation, 2R target."


def _ema(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
    return current


class StrategyRegistry:
    def __init__(self, settings) -> None:
        self._strategies: dict[str, Strategy] = {
            "baseline_v1": BaselineV1Adapter(settings),
            "trend_pullback_v1": TrendPullbackV1(settings),
        }

    def register(self, identifier: str, strategy: Strategy) -> None:
        strategy.validate_config()
        self._strategies[identifier] = strategy

    def resolve(self, identifier: str) -> Strategy:
        try:
            return self._strategies[identifier]
        except KeyError as exc:
            raise ValueError(f"unknown strategy: {identifier}") from exc

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._strategies))
