# Strategy Constitution

These are non-negotiable guardrails for APlan investment strategy work.

## Purpose

The strategy exists to convert evidence into repeatable decisions:

- which stocks enter the candidate pool;
- when to buy;
- when to sell;
- how much to hold;
- when to do nothing;
- how to learn from outcomes without overfitting.

## Authority levels

1. Strategy proposes.
2. Risk control constrains.
3. Human approves.
4. Logs preserve evidence.
5. Reviews update rules.

No strategy output is self-executing.

## Hard risk limits

Use the project risk policy when present. Current APlan defaults from the local README:

- single stock cap: 10%;
- max holdings: 10;
- known industry cap: 30%;
- max total equity exposure: 95%;
- minimum cash: 5%;
- daily turnover cap: 30%;
- portfolio drawdown circuit breaker: 15%;
- A 股 board lot: 100 shares.

Treat these as defaults, not permissions to fill every limit.

## Research gates

A strategy remains `research_only` if:

- backtest robustness fails;
- validation set fails;
- trade-date sensitivity is high;
- sample size is too small;
- costs/slippage are not modeled;
- data leakage cannot be ruled out;
- simulated or paper-trading approval is absent.

Local APlan reports currently show strategy robustness problems; do not output actionable live trades unless newer artifacts explicitly supersede this.

## Anti-overfitting rules

- Do not optimize based on one period, one year, one regime, or one trade.
- Require train/validation separation before accepting a rule.
- Check multiple rebalance offsets for date sensitivity.
- Track benchmark-relative returns, drawdown, turnover, hit rate, payoff ratio, and capacity.
- Prefer stable families of rules over isolated parameter spikes.

## Prohibited shortcuts

- “Because it looks cheap” without catalyst or quality check.
- “Because it has fallen a lot” without trend/mean-reversion evidence.
- “Because news is good” without price/volume confirmation.
- “Because backtest is high” without sample, costs, and validation.
- Averaging down without a pre-defined thesis and risk cap.

