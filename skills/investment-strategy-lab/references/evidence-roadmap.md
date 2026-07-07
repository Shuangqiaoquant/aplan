# Evidence Roadmap

Use this when improving missing fundamental, valuation, or announcement/catalyst evidence.

## Principle

Missing evidence must stay visible and conservative until the data is:

- sourced;
- timestamped;
- transformed into features;
- joined without future leakage;
- validated against outcomes.

Only then may it increase score or confidence.

## Evidence layers

### 1. Fundamental quality

Goal: identify whether the company quality supports the trade thesis.

Possible fields:

- revenue growth;
- net profit growth;
- gross/net margin trend;
- ROE/ROA;
- operating cash flow vs net profit;
- leverage and interest-bearing debt;
- earnings forecast or revision when available.

Required timestamps:

- `period_end`;
- `publish_time`;
- `source`;
- `source_hash`.

Backtests may only use records whose `publish_time` is at or before the signal timestamp.

### 2. Valuation risk

Goal: avoid paying hostile prices unless momentum/catalyst evidence justifies it.

Available starting point in APlan sync design:

- `daily_basic.pe`;
- `daily_basic.pb`;
- `daily_basic.total_mv`;
- `daily_basic.circ_mv`;
- turnover fields.

Preferred scoring:

- compare valuation to industry peers;
- compare valuation to the stock's own history;
- treat negative or meaningless PE as a special case, not as cheap.

Valuation should usually cap or penalize a thesis, not create a standalone buy.

### 3. Announcement/catalyst evidence

Goal: convert announcements into structured support/risk evidence.

Available starting point in APlan:

- `data/processed/announcements/<date>.json`;
- `data/processed/announcement_analysis/<date>.json`;
- extracted PDF text and hashes.

Use announcement evidence as:

- veto: critical/high risk, delisting, regulatory action, major litigation;
- support: buyback, order win, earnings improvement, asset restructuring, dividend, industry policy;
- uncertainty: title-only match, short text, OCR needed, unclear impact.

Never treat positive wording as positive impact without full-text or price/volume confirmation.

## Integration order

1. Add explicit `evidence_gaps` to reports.
2. Join daily valuation fields from `daily_basic`. Initial absolute PE/PB scoring is implemented; industry/history percentiles still pending.
3. Join same-day and recent announcement events with publish-time checks. Initial title-rule risk veto/penalty and full-text analysis display are implemented; positive catalyst scoring still pending.
4. Add fundamental snapshots with `period_end` and `publish_time`. Initial snapshot loading and visibility filtering are implemented; quality scoring still pending.
5. Backtest each evidence layer independently before allowing it to raise score.
6. Promote from "missing evidence" to score component only after validation.

## Current implementation status

- `daily_basic` can be rebuilt into `data/processed/valuations/<date>.csv`.
- Candidate scoring can consume valuation snapshots when supplied.
- `valuation_risk` uses conservative absolute PE/PB rules for now.
- Negative or zero PE/PB is treated as risky/invalid for cheapness, not as undervaluation.
- Missing valuation data remains visible in `evidence_gaps`.
- `data/processed/announcements/<date>.json` can be loaded into candidate scoring.
- `data/processed/announcement_analysis/<date>.json` can be loaded into candidate scoring.
- Critical announcement events cap candidates at reject/ignore; high-risk events cap candidates at watch-only.
- Negative or mixed announcement events cap candidates below paper-candidate level.
- Positive announcement events are shown as "pending full-text confirmation" and do not raise score yet.
- Full-text positive evidence is shown as "pending validation" and does not raise score yet.
- Full-text risk evidence strengthens the risk explanation.
- Announcement visibility is filtered by `published_at`.
- Fundamental snapshots can be loaded from CSV with `period_end`, `publish_time`, `source`, and `source_hash`.
- Fundamental snapshots are filtered by `publish_time`; future-published reports are invisible to earlier signals.
- Fundamental evidence is displayed and may produce risk warnings, but does not raise `quality_catalyst` yet.

Pending before valuation can be trusted as a stronger edge:

- industry-relative valuation percentiles;
- each stock's own historical valuation percentile;
- validation of valuation penalties/bonuses net of costs;
- tests for whether valuation helps by horizon and market regime.

Pending before announcement evidence can become positive catalyst score:

- full-text extraction coverage checks;
- event-specific historical outcome tests;
- price/volume confirmation rule;
- separation between title-only risk veto, full-text risk confirmation, and full-text catalyst support;
- validation that catalyst scoring improves results beyond headline chasing.

Pending before fundamental quality can become a positive score:

- reliable financial statement source integration;
- restatement and revision handling;
- industry-normalized quality comparisons;
- tests for cash-flow/profit consistency, leverage, and margin stability;
- validation that quality improves candidate outcomes by horizon without future leakage.

## Score promotion rules

Evidence can raise score only if:

- it is available before the signal;
- it has a deterministic transformation;
- it improves validation metrics net of costs;
- it does not concentrate gains in one year, one industry, or one rebalance offset.

If evidence is useful mainly for avoiding disasters, implement it as a veto or penalty before using it as a positive score.

## Validation status

An evidence validation command exists:

```text
aplan-evidence-validate
```

It compares the same baseline signals under evidence-filter variants such as:

- baseline;
- exclude_bad_valuation;
- exclude_high_risk_announcement;
- exclude_announcement_risk;
- exclude_all_evidence_risks.

Interpretation rules:

- If a variant drops zero or very few signals, do not infer that the evidence is useless.
- First check evidence coverage: how many historical signals actually had valuation, announcement, or fundamental flags.
- Promotion requires coverage plus improvement across horizons, rebalance offsets, and validation periods.
- A better-looking single offset remains research-only until the robustness gates pass.

## Alternative data sources

AkShare can be used as a low-cost supplemental source, not as an unreviewed replacement for Tushare.

Current integration:

- optional dependency: `.[akshare]`;
- command: `aplan-akshare spot-valuations`;
- raw output: `data/raw/akshare/<date>/stock_zh_a_spot_em.json`;
- processed output: `data/processed/akshare_valuations/<date>.csv`.

Rules:

- Keep AkShare-derived files separate from Tushare-derived files.
- Preserve raw snapshots and download timestamps.
- Do not mix Tushare and AkShare market-cap units without an explicit normalization check.
- Use AkShare first for coverage and cross-checking; promote it to scoring only after consistency validation.
