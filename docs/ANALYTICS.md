# Analytics and Risk Formulas

## Risk snapshot

For a position with a real stop and valid broker tick specification:

```text
price distance = abs(open price - stop loss)
risk amount = price distance / trade tick size × loss tick value × volume
risk percent = risk amount / account equity × 100
aggregate open risk = sum(position risk amount) / equity × 100
remaining risk budget = 6% - aggregate open risk
```

The policy is intentionally unambiguous: normal and hard maximum risk per individual trade is **2%**, while maximum aggregate open risk is **6%**. The 6% aggregate limit is never presented as risk per trade.

If any open position lacks a stop, tick size/value is invalid, or equity is non-positive, aggregate open risk and remaining budget are `Unavailable`; known partial risk is not misrepresented as total risk. Margin usage is `margin / equity × 100` when equity is positive.

## Closed-trade summary

- Win/loss/breakeven use net P&L greater than, less than, or equal to zero.
- Win rate = wins / all completed trades × 100.
- Gross profit/loss and net profit are sums of persisted net P&L.
- Profit factor = gross profit / absolute gross loss; unavailable without losses.
- Payoff ratio = average win / absolute average loss; unavailable without both sides.
- Currency expectancy = mean net P&L.
- R expectancy and average R = mean persisted realized R.
- Recovery factor = net profit / maximum currency drawdown; unavailable at zero drawdown.

## Drawdown

Trade drawdown uses the cumulative net-P&L curve and its running peak. The dashboard Equity/Balance/Drawdown chart does **not** relabel cumulative P&L as account equity. It reads real persisted `account_snapshots`. Account drawdown is the percentage distance between current equity and running peak equity.

## Ratios and sample thresholds

Sharpe and Sortino operate on persisted realized-R observations and remain unavailable below 30 samples. No annualization factor is asserted because trade arrival frequency is not a stable time series in Phase 1.5.

Confidence calibration buckets are `[0,50)`, `[50,60)`, `[60,70)`, `[70,80)`, `[80,90)`, and `[90,100]` percent. Empty buckets report zero trades and unavailable outcome statistics. Phase 1.5 has no AI engine, so calibration remains empty until genuine decisions are persisted.

## Time and sessions

All calculations order by UTC timestamps. Session and market-regime segmentation uses persisted labels only; missing labels map to `unavailable`. The service does not infer a trading session from local time.
