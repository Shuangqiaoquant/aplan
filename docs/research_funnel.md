# APlan Research Funnel

The research funnel links strategy intent to staged data acquisition. It does not
download every available dataset before a research question exists.

## Stages

1. `user_scope`: apply explicit user constraints such as industries, code
   prefixes, allowlists, exclusions, ChiNext, and STAR Market preferences.
   Constraints reduce the research universe but never add score.
2. `broad_screen`: use security master and daily bars for low-cost eligibility,
   liquidity, momentum, turnover, market regime, and industry-relative checks.
3. `refinement`: request only the profile-specific medium-cost data needed for
   the broad candidate pool.
4. `fine_research`: request high-cost or real-time evidence only for the small
   watch pool and emit research candidates.

Every run remains `research_only`, records all constraints and stage outputs,
and sets `execution_allowed=false`.

## First Pass

The first pass stops after the broad screen for human review:

```bash
aplan-research \
  --funnel \
  --root . \
  --bars data/processed/daily \
  --securities data/processed/securities.csv \
  --date 2026-07-22 \
  --strategy-profile hybrid \
  --broad-pool 300 \
  --refined-pool 50 \
  --top 10
```

The JSON run record is written under:

```text
runs/research_funnel/<YYYYMMDD>/<run_id>.json
```

It contains the user constraints, every stage's input and output counts, output
symbols, elimination reasons, data requirements, adapter readiness, and human
confirmation state.

## User Constraints

Preferences are explicit scope constraints:

```bash
aplan-research \
  --funnel \
  --bars data/processed/daily \
  --securities data/processed/securities.csv \
  --date 2026-07-22 \
  --strategy-profile event \
  --industries 银行,电力设备 \
  --exclude-star \
  --risk-preference conservative \
  --broad-pool 100 \
  --refined-pool 30 \
  --top 5
```

Use `--include-symbols` for a strict user allowlist and `--exclude-symbols` for
manual exclusions. Risk preference is recorded for audit but does not silently
change the score.

## Confirmed Refinement

After reviewing the broad pool and its planned data requirements, rerun the same
configuration with `--confirm-refinement`. Optional evidence paths are used only
when supplied:

```bash
aplan-research \
  --funnel \
  --confirm-refinement \
  --bars data/processed/daily \
  --securities data/processed/securities.csv \
  --date 2026-07-22 \
  --strategy-profile hybrid \
  --valuations data/processed/akshare_valuations/20260722.csv \
  --fundamentals data/processed/akshare_fundamentals/20260722.csv \
  --broad-pool 300 \
  --refined-pool 50 \
  --top 10
```

Datasets whose adapters are not implemented are recorded as planned work rather
than being treated as negative evidence. Real-time Level-1 data is always marked
`on_demand_only` and should be requested only for the final watch pool.
