# Buy Entry v0.1

Status: `research_only`

This document defines when an APlan candidate may move from research attention to paper-observation or paper-buy confirmation. It does not authorize live trading.

## Core principle

Buying is a two-step process:

1. Signal-day candidate selection identifies stocks worth attention.
2. Next-day confirmation decides whether the setup still deserves paper-buy consideration.

A high score is not enough. Eligibility gates outrank ranking scores.

## Candidate tiers

| Decision band | Meaning | Buy-entry treatment |
|---|---|---|
| `reject_ignore` | Failed screen | Ignore |
| `watch_only` | Interesting but blocked | Watch only |
| `research_candidate` | Worth research attention | Register observation; no paper-buy confirmation |
| `paper_candidate_if_validated` | Strong enough for paper consideration if validation permits | May enter next-day confirmation |
| high-priority score band | Very strong research candidate | Still blocked unless validation and confirmation pass |

While strategy status is `v0.1-research`, all real position sizes are 0%.

## Entry style rules

Every candidate must have one explicit entry style. Do not mix styles.

### Breakout continuation

Purpose: participate only when strength persists after supply is cleared.

Signal-day requirements:

- score normally at or above the paper-candidate band;
- constructive relative strength and trend/drawdown profile;
- turnover/liquidity supports entry and exit;
- no high-risk announcement, major fundamental deterioration, suspension, or limit-up/limit-down execution block.

Next-day confirmation:

- block if suspended;
- block if limit-down;
- block if next open is more than 5% above signal-day close;
- block if next close is below signal-day low;
- block if next close fails to remain above signal-day close;
- warn, but do not automatically block, if next-day turnover is below 80% of signal-day turnover.

Default paper position if later promoted:

- initial paper weight: 1%-3%;
- max single stock weight: 10%;
- no averaging down by default.

### Pullback in uptrend

Current status: watch-only until separately validated.

Reason: current local validation has not yet proved a robust pullback entry rule.

### Event confirmation

Current status: watch-only unless price/volume confirms and announcement/fundamental evidence is available.

Reason: title-level event classification is not enough to justify buying.

### Mean reversion

Current status: unvalidated watch-only.

Reason: oversold rebound logic must be tested separately and should not be mixed with breakout continuation.

## Cancel conditions

Cancel or keep watching when any applies:

- data is missing or stale;
- score falls below paper-candidate band;
- decision band is `research_candidate` or lower;
- entry style is unclear or unvalidated;
- known high-risk announcement is unresolved;
- multiple important evidence gaps remain;
- next-day gap exceeds the chase limit;
- next-day price action invalidates the signal;
- portfolio risk policy would be breached.

## Review metrics

Track these before changing the rule:

- next-day confirmation pass rate;
- 1/5/20-day returns for confirmed vs blocked candidates;
- gap-open bucket performance;
- turnover-confirmed vs turnover-weak performance;
- performance by market regime and industry;
- missed winners caused by the 5% gap cap;
- avoided losers caused by the 5% gap cap.

Do not update the rule from one outcome. Review after at least 20 comparable signals or a clear execution/risk failure.
