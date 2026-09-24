"""Deterministic, read-only Pair Zone V1 strategy plugin.

Pair zones are derived only from closed M15 candles available at the replay
timestamp.  The strategy has no broker/API write path and emits ordinary
``ShadowDecision`` objects so the existing research/live plumbing remains
pluggable.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from models.market import Candle, MarketSnapshot, Timeframe
from models.shadow import ShadowAction, ShadowDecision
from services.features import FeatureValidationError
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_risk_gate import ShadowRiskGate
from services.strategy_platform import StrategyMetadata, _ema, _load_config, config_hash


@dataclass(frozen=True, slots=True)
class PairZone:
    zone_id: str
    direction: ShadowAction
    created_at: Any
    upper_bound: float
    lower_bound: float
    first_timestamp: Any
    second_timestamp: Any
    first_ohlc: dict[str, float]
    second_ohlc: dict[str, float]
    zone_width: float
    displacement: float
    body_ratio: float
    overlap_ratio: float


class PairZoneV1:
    """Initial Pair Zone hypothesis; all transitions are causal and explicit."""

    def __init__(self, settings, *, config_path: Path | None = None) -> None:
        path = config_path or Path(__file__).resolve().parents[1] / "config" / "strategies" / "pair_zone_v1.yaml"
        self.config = _load_config(path)
        self.metadata = StrategyMetadata(
            strategy_id=str(self.config["strategy_id"]),
            strategy_version=str(self.config["strategy_version"]),
            config_version=str(self.config["config_version"]),
            config_hash=config_hash(self.config),
            display_name="Pair Zone V1 (Research)",
        )
        self.target_rr = float(self.config["target_rr"])
        self._seen_pairs: set[str] = set()
        self._pair_cache: dict[str, PairZone | None] = {}
        self._seen_zones: dict[str, PairZone] = {}
        self._touch_counts: dict[str, int] = {}
        self._confirmed: set[str] = set()
        # Canonical zone IDs bind the M15 pair timestamps and direction. Keep
        # invalidation proof only until that zone's canonical age horizon ends.
        self._invalidated: dict[str, Any] = {}
        self._expired: set[str] = set()
        self._touch_events = 0
        self.validate_config()

    @property
    def strategy_version(self) -> str:
        return self.metadata.strategy_version

    def required_timeframes(self) -> tuple[Timeframe, ...]:
        return (Timeframe.H1, Timeframe.M15, Timeframe.M5)

    def validate_config(self) -> None:
        if self.target_rr <= 0:
            raise ValueError("pair zone target RR must be greater than zero")
        if float(self.config["min_body_points"]) <= 0 or float(self.config["minimum_zone_width"]) <= 0:
            raise ValueError("pair zone size constraints must be positive")
        if int(self.config["zone_max_age_minutes"]) <= 0 or int(self.config["zone_max_touches"]) <= 0:
            raise ValueError("pair zone lifecycle limits must be positive")
        if str(self.config["invalidation_close_rule"]) != "opposite_zone_boundary":
            raise ValueError("unsupported pair zone invalidation rule")

    def reset_research(self) -> None:
        self._seen_pairs.clear()
        self._pair_cache.clear()
        self._seen_zones.clear()
        self._touch_counts.clear()
        self._confirmed.clear()
        self._invalidated.clear()
        self._expired.clear()
        self._touch_events = 0

    def research_statistics(self) -> dict[str, object]:
        return {
            "candidate_pairs": len(self._seen_pairs),
            "valid_zones": len(self._seen_zones),
            "invalid_zones": max(0, len(self._seen_pairs) - len(self._seen_zones)),
            "zone_touches": self._touch_events,
            "confirmed_entries": len(self._confirmed),
            "invalidated_zones": len(self._invalidated),
            "expired_zones": len(self._expired),
            "lifecycle_states": {
                "CREATED": len(self._seen_zones),
                "ACTIVE": max(0, len(self._seen_zones) - len(self._invalidated) - len(self._expired)),
                "TOUCHED": sum(count > 0 for count in self._touch_counts.values()),
                "CONFIRMED": len(self._confirmed),
                "INVALIDATED": len(self._invalidated),
                "EXPIRED": len(self._expired),
            },
        }

    @staticmethod
    def _ohlc(candle: Candle) -> dict[str, float]:
        return {"open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close}

    def detect_pair(self, first: Candle, second: Candle) -> PairZone | None:
        """Return a zone only for a valid consecutive closed M15 pair."""
        if second.timestamp - first.timestamp != timedelta(minutes=15):
            return None
        first_range = first.high - first.low
        second_range = second.high - second.low
        first_body = abs(first.close - first.open)
        second_body = abs(second.close - second.open)
        if min(first_body, second_body) < float(self.config["min_body_points"]):
            return None
        if first_range <= 0 or second_range <= 0:
            return None
        if first_body / first_range > float(self.config["base_body_ratio_max"]):
            return None
        if second_body / second_range < float(self.config["displacement_body_ratio_min"]):
            return None
        overlap_lower = max(first.low, second.low)
        overlap_upper = min(first.high, second.high)
        overlap_width = overlap_upper - overlap_lower
        overlap_ratio = overlap_width / min(first_range, second_range)
        if overlap_width < float(self.config["minimum_zone_width"]):
            return None
        if overlap_ratio < float(self.config["minimum_overlap_ratio"]):
            return None
        displacement = abs(second.close - first.close)
        if displacement < float(self.config["displacement_min_points"]):
            return None
        if second.close > second.open and second.close > first.close:
            direction = ShadowAction.BUY
        elif second.close < second.open and second.close < first.close:
            direction = ShadowAction.SELL
        else:
            return None
        zone_key = f"{first.timestamp.isoformat()}|{second.timestamp.isoformat()}|{direction.value}"
        zone_id = "pz-" + hashlib.sha256(zone_key.encode("utf-8")).hexdigest()[:16]
        return PairZone(
            zone_id=zone_id, direction=direction,
            created_at=second.timestamp + timedelta(minutes=15),
            upper_bound=overlap_upper, lower_bound=overlap_lower,
            first_timestamp=first.timestamp, second_timestamp=second.timestamp,
            first_ohlc=self._ohlc(first), second_ohlc=self._ohlc(second),
            zone_width=overlap_width, displacement=displacement,
            body_ratio=second_body / second_range, overlap_ratio=overlap_ratio,
        )

    def _zones(self, snapshot: MarketSnapshot, timestamp) -> list[PairZone]:
        candles = snapshot.candles.get(Timeframe.M15, ())
        zones: list[PairZone] = []
        for first, second in zip(candles, candles[1:], strict=False):
            pair_key = f"{first.timestamp.isoformat()}|{second.timestamp.isoformat()}"
            self._seen_pairs.add(pair_key)
            if pair_key not in self._pair_cache:
                self._pair_cache[pair_key] = self.detect_pair(first, second)
            zone = self._pair_cache[pair_key]
            if zone is not None:
                self._seen_zones[zone.zone_id] = zone
                if zone.created_at <= timestamp:
                    zones.append(zone)
        return zones

    @staticmethod
    def _h1_direction(snapshot: MarketSnapshot, config: dict[str, Any]) -> ShadowAction | None:
        candles = snapshot.candles.get(Timeframe.H1, ())
        slow_period = int(config["htf_slow_period"])
        fast_period = int(config["htf_fast_period"])
        if len(candles) < slow_period + 1:
            return None
        closes = [c.close for c in candles if c.close > 0]
        fast = _ema(closes[-fast_period:], fast_period)
        previous_fast = _ema(closes[-fast_period - 1:-1], fast_period)
        slow = _ema(closes[-slow_period:], slow_period)
        if fast > slow and fast > previous_fast:
            return ShadowAction.BUY
        if fast < slow and fast < previous_fast:
            return ShadowAction.SELL
        return None

    def _lifecycle(self, zone: PairZone, m5: tuple[Candle, ...], timestamp) -> tuple[str, int, Candle | None]:
        max_age = timedelta(minutes=int(self.config["zone_max_age_minutes"]))
        self._prune_invalidated(timestamp)
        if timestamp < zone.created_at:
            return "CREATED", 0, None
        if zone.zone_id in self._invalidated:
            return "INVALIDATED", self._touch_counts.get(zone.zone_id, 0), None
        relevant = [c for c in m5 if zone.created_at < c.timestamp <= timestamp]
        touches = 0
        inside = False
        first_confirmation: Candle | None = None
        for candle in relevant:
            intersects = candle.high >= zone.lower_bound and candle.low <= zone.upper_bound
            if intersects and not inside:
                touches += 1
            inside = intersects
            if zone.direction == ShadowAction.BUY:
                invalid = candle.close < zone.lower_bound
                body = candle.close - candle.open
                wick = min(candle.open, candle.close) - candle.low
                confirms = intersects and candle.close > zone.upper_bound and body > 0 and wick >= body * float(self.config["confirmation_wick_to_body"])
            else:
                invalid = candle.close > zone.upper_bound
                body = candle.open - candle.close
                wick = candle.high - max(candle.open, candle.close)
                confirms = intersects and candle.close < zone.lower_bound and body > 0 and wick >= body * float(self.config["confirmation_wick_to_body"])
            if invalid:
                self._invalidated[zone.zone_id] = zone.created_at + max_age
                return "INVALIDATED", touches, None
            if first_confirmation is None and confirms:
                first_confirmation = candle
        age = timestamp - zone.created_at
        if age > timedelta(minutes=int(self.config["zone_max_age_minutes"])) or touches > int(self.config["zone_max_touches"]):
            self._expired.add(zone.zone_id)
            return "EXPIRED", touches, None
        if first_confirmation is not None:
            self._confirmed.add(zone.zone_id)
            return "CONFIRMED", touches, first_confirmation
        if touches:
            return "TOUCHED", touches, None
        return "ACTIVE", 0, None

    def _prune_invalidated(self, timestamp) -> None:
        """Forget invalidation only after canonical zone-age expiry."""

        expired_ids = [
            zone_id
            for zone_id, relevant_until in self._invalidated.items()
            if timestamp > relevant_until
        ]
        for zone_id in expired_ids:
            del self._invalidated[zone_id]

    @staticmethod
    def _has_complete_zone_m5_history(
        zone: PairZone, m5: tuple[Candle, ...], timestamp
    ) -> bool:
        """Verify the rolling M5 input covers every canonical lifecycle bar."""

        expected_first = zone.created_at + timedelta(minutes=5)
        if timestamp < expected_first:
            return True
        relevant = tuple(c for c in m5 if zone.created_at < c.timestamp <= timestamp)
        return bool(
            relevant
            and relevant[0].timestamp == expected_first
            and relevant[-1].timestamp == timestamp
            and all(
                right.timestamp - left.timestamp == timedelta(minutes=5)
                for left, right in zip(relevant, relevant[1:], strict=False)
            )
        )

    def evaluate(self, snapshot: MarketSnapshot, *, market_snapshot_id=None, risk=None,
                 data_freshness="LIVE", mt5_state="CONNECTED", runtime_state="CONNECTED",
                 candles_are_closed=False):
        timestamp = ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=candles_are_closed)
        self._prune_invalidated(timestamp)
        common = dict(market_snapshot_id=market_snapshot_id, symbol=snapshot.symbol.name,
                      m5_candle_timestamp=timestamp, strategy_name="pair_zone_v1",
                      strategy_version=self.metadata.strategy_version,
                      config_version=self.metadata.config_version, config_hash=self.metadata.config_hash,
                      data_freshness=data_freshness, execution_allowed=False)
        if not candles_are_closed:
            return self._no_trade(common, "DATA_INCOMPLETE", "DATA")
        if data_freshness != "LIVE":
            return self._no_trade(common, "DATA_STALE", "DATA")
        if mt5_state != "CONNECTED" or runtime_state != "CONNECTED":
            return self._no_trade(common, "RUNTIME_NOT_CONNECTED", "CONTEXT")
        if risk is None:
            return self._no_trade(common, "RISK_STATE_UNKNOWN", "RISK_GATE")
        try:
            m5 = snapshot.candles[Timeframe.M5]
            zones = self._zones(snapshot, timestamp)
        except (KeyError, FeatureValidationError):
            return self._no_trade(common, "DATA_INCOMPLETE", "DATA")
        candidates: list[tuple[PairZone, Candle]] = []
        h1_direction = self._h1_direction(snapshot, self.config)
        max_age = timedelta(minutes=int(self.config["zone_max_age_minutes"]))
        m15_candles = snapshot.candles.get(Timeframe.M15, ())
        coverage_cutoff = timestamp - max_age - timedelta(minutes=30)
        relevant_m15 = tuple(c for c in m15_candles if c.timestamp >= coverage_cutoff)
        coverage_starts_in_time = bool(
            relevant_m15
            and relevant_m15[0].timestamp - coverage_cutoff <= timedelta(minutes=15)
        )
        m15_coverage_complete = (
            len(relevant_m15) >= 2
            and coverage_starts_in_time
            and all(
                right.timestamp - left.timestamp == timedelta(minutes=15)
                for left, right in zip(relevant_m15, relevant_m15[1:], strict=False)
            )
        )
        recent_zones = sorted(
            (zone for zone in zones if timestamp - zone.created_at <= max_age),
            key=lambda item: item.created_at,
            reverse=True,
        )[:24]
        htf_mismatch = False
        observed_zones: list[tuple[PairZone, str, int]] = []
        for zone in recent_zones:
            already_confirmed = zone.zone_id in self._confirmed
            state, touches, confirmation = self._lifecycle(zone, m5, timestamp)
            observed_zones.append((zone, state, touches))
            previous = self._touch_counts.get(zone.zone_id, 0)
            if touches > previous:
                self._touch_events += touches - previous
                self._touch_counts[zone.zone_id] = touches
            if (not already_confirmed and state == "CONFIRMED" and confirmation is not None
                    and confirmation.timestamp == timestamp):
                if h1_direction != zone.direction:
                    htf_mismatch = True
                    continue
                candidates.append((zone, confirmation))
        if not candidates:
            active_zones = [
                item for item in observed_zones
                if item[1] in {"CREATED", "ACTIVE", "TOUCHED", "CONFIRMED"}
            ]
            selected_zone = active_zones[0] if active_zones else None
            if htf_mismatch or (zones and h1_direction is None):
                reason = "HTF_DIRECTION_MISMATCH"
                stage = "HTF_DIRECTION"
            elif zones:
                reason = "ZONE_CONFIRMATION_MISSING"
                stage = "ZONE_CONFIRMATION"
            else:
                reason = "NO_VALID_ZONE"
                stage = "PAIR_ZONE"
            if selected_zone is not None:
                if self._has_complete_zone_m5_history(
                    selected_zone[0], m5, timestamp
                ):
                    observation = self._zone_observation(
                        "ACTIVE_ZONE", reason, selected_zone[0], selected_zone[1]
                    )
                else:
                    observation = self._zone_observation(
                        "UNKNOWN", "INCOMPLETE_ZONE_M5_LIFECYCLE_COVERAGE"
                    )
            elif not m15_coverage_complete:
                observation = self._zone_observation(
                    "UNKNOWN",
                    "INSUFFICIENT_M15_COVERAGE"
                    if len(relevant_m15) < 2 or not coverage_starts_in_time
                    else "M15_CANDLE_GAP",
                )
            else:
                observation = self._zone_observation(
                    "HEALTHY_NO_ACTIVE_ZONE",
                    "NO_CURRENT_ACTIVE_ZONE" if zones else "NO_VALID_ZONE",
                )
            return self._no_trade(common, reason, stage, observation=observation)
        zone, confirmation = sorted(candidates, key=lambda item: item[0].zone_id)[0]
        observation = (
            self._zone_observation(
                "ACTIVE_ZONE", "CANONICAL_SIGNAL_GENERATED", zone, "CONFIRMED"
            )
            if self._has_complete_zone_m5_history(zone, m5, timestamp)
            else self._zone_observation(
                "UNKNOWN", "INCOMPLETE_ZONE_M5_LIFECYCLE_COVERAGE"
            )
        )
        if zone.zone_id in self._confirmed and zone.zone_id in self._invalidated:
            return self._no_trade(
                common, "ZONE_INVALIDATED", "ZONE_LIFECYCLE",
                observation=self._zone_observation(
                    "HEALTHY_NO_ACTIVE_ZONE", "ZONE_INVALIDATED"
                ),
            )
        entry = confirmation.close
        stop = zone.lower_bound - float(self.config["stop_buffer_points"]) if zone.direction == ShadowAction.BUY else zone.upper_bound + float(self.config["stop_buffer_points"])
        distance = entry - stop if zone.direction == ShadowAction.BUY else stop - entry
        if distance <= snapshot.symbol.trade_tick_size:
            return self._no_trade(common, "INVALID_SL", "GEOMETRY", observation=observation)
        if snapshot.symbol.spread > int(self.config["max_spread_points"]):
            return self._no_trade(common, "SPREAD_TOO_HIGH", "SPREAD", observation=observation)
        target = entry + distance * self.target_rr if zone.direction == ShadowAction.BUY else entry - distance * self.target_rr
        gate, volume = ShadowRiskGate(max_trade_risk_percent=2, max_aggregate_risk_percent=6).evaluate(snapshot, risk, entry=entry, stop=stop)
        if not gate.approved or volume is None:
            return self._no_trade(
                common, gate.reason_codes[0], "RISK_GATE", observation=observation
            )
        return ShadowDecision(
            **common, decision=zone.direction,
            market_regime="TREND_UP" if zone.direction == ShadowAction.BUY else "TREND_DOWN",
            entry_price=entry, stop_loss=stop, take_profit=target,
            risk_reward_ratio=self.target_rr, requested_risk_percent=2.0,
            approved_risk_percent=gate.proposed_risk_percent, hypothetical_volume=volume,
            confidence=0.55, reason_codes=("PAIR_ZONE_CONFIRMED",),
            human_readable_reason="Closed M15 pair zone touched and confirmed by a closed M5 rejection candle.",
            risk_gate_state=gate.state,
            feature_context={"zone_id": zone.zone_id, "zone_state": "CONFIRMED",
                             "zone_direction": zone.direction.value,
                             "zone_lower": zone.lower_bound, "zone_upper": zone.upper_bound,
                             "zone_created_at": zone.created_at.isoformat(),
                             "pair_first_timestamp": zone.first_timestamp.isoformat(),
                             "pair_second_timestamp": zone.second_timestamp.isoformat(),
                             "zone_width": zone.zone_width, "displacement": zone.displacement,
                             "overlap_ratio": zone.overlap_ratio,
                             "pair_zone_observation": observation},
        )

    @staticmethod
    def _zone_observation(state, reason, zone=None, lifecycle_state=None):
        return {
            "state": state,
            "reason": reason,
            "direction": zone.direction.value if zone is not None else None,
            "zone_id": zone.zone_id if zone is not None else None,
            "zone_lower": zone.lower_bound if zone is not None else None,
            "zone_upper": zone.upper_bound if zone is not None else None,
            "zone_lifecycle_state": lifecycle_state,
        }

    @staticmethod
    def _no_trade(common, reason, stage, *, observation=None):
        current_observation = observation or PairZoneV1._zone_observation(
            "UNKNOWN", reason
        )
        return ShadowDecision(
            **common, decision=ShadowAction.NO_TRADE, market_regime="UNCERTAIN",
            reason_codes=(reason,), human_readable_reason=reason.replace("_", " ").title(),
            risk_gate_state="NOT_EVALUATED",
            feature_context={
                "rejection_stage": stage,
                "pair_zone_observation": current_observation,
            },
        )

    def explain(self) -> str:
        return ("Pair Zone V1: closed M15 range-overlap pair with directional displacement, "
                "H1 EMA direction filter, and closed M5 rejection-close confirmation.")
