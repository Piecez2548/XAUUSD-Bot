"""Pure analytics formulas with explicit insufficient-sample behavior."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from statistics import fmean, stdev
from typing import Any


@dataclass(frozen=True, slots=True)
class TradeSample:
    trade_id: str
    direction: str
    exit_time: datetime
    net_profit: float
    realized_r: float | None = None
    duration_seconds: int | None = None
    mae: float | None = None
    mfe: float | None = None
    session: str | None = None
    market_regime: str | None = None
    strategy_version: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    confidence: float | None = None


def _optional_mean(values: Iterable[float | int | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return fmean(present) if present else None


class AnalyticsService:
    MIN_RATIO_SAMPLE = 30

    def summary(self, trades: Sequence[TradeSample]) -> dict[str, Any]:
        ordered = sorted(trades, key=lambda trade: trade.exit_time)
        profits = [trade.net_profit for trade in ordered]
        wins = [value for value in profits if value > 0]
        losses = [value for value in profits if value < 0]
        breakeven = [value for value in profits if value == 0]
        realized_rs = [trade.realized_r for trade in ordered if trade.realized_r is not None]

        gross_profit = sum(wins)
        gross_loss = sum(losses)
        net_profit = sum(profits)
        average_win = fmean(wins) if wins else None
        average_loss = fmean(losses) if losses else None
        drawdown = self.drawdown_curve(ordered)
        max_drawdown = max((item["drawdown"] for item in drawdown), default=0.0)
        current_drawdown = drawdown[-1]["drawdown"] if drawdown else 0.0
        max_wins, max_losses = self._consecutive_streaks(profits)

        return {
            "total_trades": len(ordered),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "win_rate": len(wins) / len(ordered) * 100 if ordered else None,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "net_profit": net_profit,
            "average_win": average_win,
            "average_loss": average_loss,
            "average_r": fmean(realized_rs) if realized_rs else None,
            "expectancy_r": fmean(realized_rs) if realized_rs else None,
            "expectancy_currency": fmean(profits) if profits else None,
            "profit_factor": gross_profit / abs(gross_loss) if gross_loss < 0 else None,
            "payoff_ratio": (
                average_win / abs(average_loss)
                if average_win is not None and average_loss not in (None, 0)
                else None
            ),
            "max_drawdown": max_drawdown,
            "current_drawdown": current_drawdown,
            "maximum_consecutive_wins": max_wins,
            "maximum_consecutive_losses": max_losses,
            "average_trade_duration_seconds": _optional_mean(
                trade.duration_seconds for trade in ordered
            ),
            "average_mae": _optional_mean(trade.mae for trade in ordered),
            "average_mfe": _optional_mean(trade.mfe for trade in ordered),
            "recovery_factor": net_profit / max_drawdown if max_drawdown > 0 else None,
            "sharpe_ratio": self._sharpe(realized_rs),
            "sortino_ratio": self._sortino(realized_rs),
            "ratio_sample_size": len(realized_rs),
        }

    @staticmethod
    def equity_curve(trades: Sequence[TradeSample]) -> list[dict[str, Any]]:
        equity = 0.0
        result: list[dict[str, Any]] = []
        for trade in sorted(trades, key=lambda item: item.exit_time):
            equity += trade.net_profit
            result.append(
                {
                    "trade_id": trade.trade_id,
                    "timestamp": trade.exit_time.isoformat(),
                    "cumulative_net_profit": equity,
                }
            )
        return result

    @classmethod
    def drawdown_curve(cls, trades: Sequence[TradeSample]) -> list[dict[str, Any]]:
        peak = 0.0
        result: list[dict[str, Any]] = []
        for point in cls.equity_curve(trades):
            equity = float(point["cumulative_net_profit"])
            peak = max(peak, equity)
            result.append(
                {
                    "trade_id": point["trade_id"],
                    "timestamp": point["timestamp"],
                    "drawdown": peak - equity,
                }
            )
        return result

    def segment(
        self,
        trades: Sequence[TradeSample],
        key: Callable[[TradeSample], str | None],
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[TradeSample]] = defaultdict(list)
        for trade in trades:
            groups[key(trade) or "unavailable"].append(trade)
        return [
            {"segment": name, **self.summary(items)}
            for name, items in sorted(groups.items())
        ]

    def confidence_calibration(
        self, trades: Sequence[TradeSample]
    ) -> list[dict[str, Any]]:
        buckets = ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0))
        result: list[dict[str, Any]] = []
        for lower, upper in buckets:
            included = [
                trade
                for trade in trades
                if trade.confidence is not None
                and trade.confidence >= lower
                and (trade.confidence < upper or (upper == 1.0 and trade.confidence <= upper))
            ]
            summary = self.summary(included)
            result.append(
                {
                    "bucket": f"{int(lower * 100)}-{int(upper * 100)}%",
                    "trade_count": summary["total_trades"],
                    "win_rate": summary["win_rate"],
                    "average_r": summary["average_r"],
                    "expectancy_r": summary["expectancy_r"],
                    "profit_factor": summary["profit_factor"],
                }
            )
        return result

    @staticmethod
    def _consecutive_streaks(profits: Sequence[float]) -> tuple[int, int]:
        max_wins = max_losses = wins = losses = 0
        for profit in profits:
            if profit > 0:
                wins += 1
                losses = 0
            elif profit < 0:
                losses += 1
                wins = 0
            else:
                wins = losses = 0
            max_wins = max(max_wins, wins)
            max_losses = max(max_losses, losses)
        return max_wins, max_losses

    def _sharpe(self, returns: Sequence[float]) -> float | None:
        if len(returns) < self.MIN_RATIO_SAMPLE:
            return None
        deviation = stdev(returns)
        return fmean(returns) / deviation if deviation > 0 else None

    def _sortino(self, returns: Sequence[float]) -> float | None:
        if len(returns) < self.MIN_RATIO_SAMPLE:
            return None
        downside = [min(0.0, value) for value in returns]
        downside_deviation = sqrt(fmean(value * value for value in downside))
        return fmean(returns) / downside_deviation if downside_deviation > 0 else None
