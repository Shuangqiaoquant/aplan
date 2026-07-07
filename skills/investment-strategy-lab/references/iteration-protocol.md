# Iteration Protocol

Use this when the user reports gains/losses, paper-trade results, missed trades, or asks to update rules.

## Trade feedback record

Ask for or reconstruct:

```text
Ticker:
Strategy version:
Signal date:
Entry date/price:
Exit date/price or current mark:
Position size:
Original thesis:
Original invalidation:
Actual outcome:
Benchmark outcome:
Costs/slippage:
Rule followed? yes/no:
If no, what changed:
Lesson candidate:
```

## Review cadence

Do not update core rules from isolated outcomes.

Suggested cadence:

- quick note after every trade;
- mini-review every 5-10 completed trades or every month;
- formal version review every 20+ comparable trades or after a clear regime change;
- emergency review after risk breach, data error, or execution failure.

## Diagnosis categories

Classify each outcome before changing rules:

1. Good process, good outcome.
2. Good process, bad outcome.
3. Bad process, good outcome.
4. Bad process, bad outcome.
5. Data/execution failure.
6. Market regime mismatch.

Only categories 3, 4, 5, and repeated 6 usually justify process changes.

## Rule update template

```text
Proposed version:
Old rule:
Observed issue:
Evidence sample:
New rule:
Expected improvement:
Possible new failure:
Validation method:
Rollback condition:
```

## Promotion gates

Research → paper:

- passes local validation;
- date sensitivity acceptable;
- costs/slippage modeled;
- user explicitly approves paper simulation.

Paper → live candidate:

- enough paper trades across market conditions;
- drawdown within policy;
- no major rule violations;
- user explicitly approves live consideration.

Live candidate → live:

- separate explicit approval;
- broker/execution workflow and risk kill-switch defined;
- position and order plan reviewed.

