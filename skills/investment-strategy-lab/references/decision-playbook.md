# Decision Playbook

Use this when generating candidate, buy, sell, hold, or position-sizing proposals.

## Candidate funnel

A stock can enter the candidate pool only when it passes enough evidence checks for its horizon.

Minimum checks:

1. Tradability: not suspended, not blocked by limit-up/limit-down execution assumptions, sufficient liquidity.
2. Data integrity: latest data date and source hash are known when available.
3. Horizon match: short-term, swing, or medium-term; do not blend labels.
4. Relative merit: compare with benchmark and peers, not only its own history.
5. Risk: known event, announcement, industry, liquidity, and drawdown risks.

## Evidence stack

Prefer evidence in this order:

1. Validation status from APlan artifacts.
2. Price/volume and relative strength.
3. Fundamental quality or earnings/cash-flow change.
4. Announcement/catalyst evidence.
5. Valuation and sentiment as supporting context.

Technical indicators are seasoning, not the main thesis.

## v0.1 scorecard

Use this scorecard for research ranking and explanation. It is not a validated live model.

The score answers: "Which candidates deserve attention first?" It does not by itself authorize buying.

Use a two-layer decision structure:

1. Eligibility layer: market regime, industry weakness, announcement/fundamental risk, evidence gaps, concentration, and entry-style clarity decide whether a candidate may progress beyond watch-only.
2. Ranking layer: score components rank candidates that deserve research attention.

A high ranking score cannot override a failed eligibility check.

### One-vote vetoes

Reject or downgrade to watch-only when any applies:

- insufficient or stale data;
- suspension, delisting/ST-like risk, or unresolved major abnormality;
- likely untradeable next session because of limit-up/limit-down assumptions;
- liquidity too weak for planned size;
- known critical/high-risk announcement not yet reviewed;
- visible fundamental deterioration, especially multiple concurrent red flags;
- thesis depends on unavailable future data;
- position would breach portfolio/risk policy.
- single-stock concentration exceeds policy limits; all-in single-stock exposure is a risk failure, not a conviction signal.

### Base score dimensions

Use 0-100, with transparent components:

```text
relative_strength: 0-30
trend_drawdown:    0-20
liquidity_flow:    0-15
quality_catalyst:  0-15
valuation_risk:    0-10
execution_fit:     0-10
```

Interpretation:

- `relative_strength`: outperforming benchmark/peers over the chosen horizon.
- `trend_drawdown`: trend is constructive and drawdown is controlled.
- `liquidity_flow`: turnover/liquidity supports entry and exit.
- `quality_catalyst`: fundamentals, announcements, or events support the thesis.
- `valuation_risk`: valuation is not obviously hostile for the horizon.
- `execution_fit`: next-session execution is plausible after costs, lot size, and limits.

### Current APlan factor bridge

If only current local quantitative factors are available, map them conservatively:

```text
existing momentum rank        -> relative_strength
existing low-volatility rank  -> trend_drawdown
existing turnover trend       -> liquidity_flow
missing fundamentals/events   -> quality_catalyst capped at neutral/low
missing valuation             -> valuation_risk capped at neutral/low
execution checks              -> execution_fit
```

Do not invent unavailable fundamentals, valuation, or event evidence. Mark them as missing.

Current fundamental handling is asymmetric by design:

- clean fundamentals do not add positive score yet;
- one visible red flag can cap the candidate below paper-candidate level;
- multiple visible red flags cap the candidate at watch-only;
- these caps are risk controls, not predictions that price must fall.

### Confidence

Confidence is evidence quality, not optimism.

```text
0.20-0.40: thin evidence or missing important fields
0.40-0.60: usable quantitative evidence, limited qualitative confirmation
0.60-0.75: multiple independent evidence types align
0.75+: reserved for validated strategy plus clean current evidence
```

While strategy status is research-only, avoid confidence above 0.60 unless explaining a non-trading research ranking.

### Decision bands

```text
score < 50: reject / ignore
50-65: watch only
65-75: research candidate
75-85: paper candidate if validation permits
85+: high-priority candidate, still blocked by validation/approval gates
```

In `research_only`, all bands still imply 0% real position.

## Buy proposal

A buy proposal must define:

- entry trigger;
- planned entry date assumption;
- initial position;
- maximum position;
- stop/invalidation condition;
- expected holding horizon;
- thesis review date;
- evidence that would cancel the buy.

Before a paper buy is confirmed, run the next-day confirmation gate:

- block if the next opening gap exceeds the chase limit;
- block if the next close invalidates the signal by breaking the signal-day low;
- block breakout-continuation candidates that fail to hold above the signal-day close;
- downgrade candidates when next-day turnover does not confirm the breakout;
- keep the candidate pending when next-day data is missing.

Default position posture while strategy is unvalidated:

- research only: 0%;
- paper-trade candidate: small notional paper weight;
- validated candidate: use risk policy and confidence score;
- live candidate: only after explicit user approval and live-trading framework.

### Entry styles to label explicitly

Every buy candidate should identify one entry style:

- breakout continuation: buying strength after supply is cleared;
- pullback in uptrend: buying weakness inside a still-valid trend;
- event confirmation: buying after price/volume confirms a catalyst;
- mean reversion: buying oversold conditions, only if separately validated.

Do not mix entry styles in one rule. If the style is unclear, keep the candidate on watch.

## Add / reduce / sell

Add only if:

- original thesis is working;
- risk has not increased disproportionately;
- portfolio caps allow it;
- the new entry has its own favorable expected return.

Reduce if:

- position exceeds risk budget;
- catalyst has played out;
- risk/reward worsens;
- better candidates displace it;
- market regime conflicts with the holding horizon.

Sell if:

- thesis is invalidated;
- stop or circuit breaker triggers;
- initial stop loss, trailing stop, or time stop triggers;
- data shows fundamental deterioration;
- trend or relative strength breaks for trend-following rules;
- target/review condition is reached and expected return is no longer attractive.

Do not label a sell as a prediction that price must fall. It is an exit discipline.

## Action matrix

Use this matrix before proposing any action:

| Current state | Evidence improves | Evidence flat | Evidence worsens |
|---|---|---|---|
| no position | watch / candidate | watch | reject |
| paper position | hold / add only if preplanned | hold | reduce / exit |
| live position | hold / risk-reviewed add | hold / review | reduce / exit |

For research-only work, translate all actions into hypothetical signals and required evidence, not real orders.

## Position sizing

Base size on:

- strategy status;
- confidence and evidence quality;
- volatility/drawdown risk;
- liquidity and execution risk;
- correlation with existing holdings;
- remaining cash and turnover budget.

A simple starting template:

```text
research_only: 0%
paper_trade_candidate: 1% to 3% paper weight
validated_candidate: 3% to 7% target weight
high-conviction validated: up to policy cap, only with explicit approval
```

Use smaller size when invalidation is far away or volatility is high.

### Concentration discipline

In research and paper stages, concentration risk is handled before signal optimism:

- unvalidated strategies should not justify concentrated single-name exposure;
- any single stock above policy cap must be flagged before discussing adds;
- all-in single-stock exposure should be converted into a risk-reduction plan;
- loss-making concentrated positions should not be averaged down unless a separately validated recovery rule exists.
