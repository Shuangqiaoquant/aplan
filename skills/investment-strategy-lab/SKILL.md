---
name: investment-strategy-lab
description: Use this skill when researching, designing, reviewing, or iterating APlan investment strategies, including stock selection, buy/sell timing, position sizing, risk controls, paper-trading feedback, and strategy version updates. The skill is for evidence-based investment research and workflow integration, not guaranteed returns or autonomous trading.
---

# Investment Strategy Lab

This skill turns investment discussion into a controlled research loop for APlan. It helps decide what to buy, when to buy, when to sell, and how much to hold, while keeping every rule auditable and updateable from real feedback.

Core stance: produce evidence-backed strategy proposals, not financial promises. Keep the human as final approver.

## Default scope

- Market: A 股 / APlan research workflow unless the user explicitly changes market.
- Current project state: research only until validation and paper-trading gates pass.
- Execution assumption: signals form after close; earliest simulated execution is next tradable open.
- Never treat one profitable or losing trade as enough evidence to rewrite a strategy.

## When using this skill

1. Identify the task type:
   - strategy design
   - stock candidate review
   - buy/sell/position proposal
   - trade feedback ingestion
   - periodic review
   - strategy version update
2. Load only the needed reference:
   - For hard limits and non-negotiables, read `references/constitution.md`.
   - For the current baseline strategy specification, read `references/v0.1-strategy-spec.md`.
   - For missing evidence and data integration work, read `references/evidence-roadmap.md`.
   - For candidate/buy/sell/position workflow, read `references/decision-playbook.md`.
   - For user feedback, paper trades, and rule updates, read `references/iteration-protocol.md`.
3. Check current APlan artifacts before making strategy claims when local files exist:
   - `README.md`
   - `docs/architecture.md`
   - `reports/backtest_v*/report.md`
   - `reports/horizon_validation/report.md`
   - latest `reports/daily/<date>/*.md`
   - latest `runs/daily/<date>/*.json`
4. State the strategy version and whether the output is:
   - `research_only`
   - `paper_trade_candidate`
   - `validated_candidate`
   - `live_candidate`

## Required output shape for any buy/sell/hold proposal

Use this compact structure:

```text
Status:
Strategy version:
Decision:
Candidate:
Time horizon:
Evidence:
Invalidation:
Position:
Risk controls:
What would change my mind:
Record needed after outcome:
```

If evidence is insufficient, say so and output what evidence is missing instead of forcing a decision.

## Strategy versioning rules

- Start at `v0.1-research` unless a documented version exists.
- A version change requires:
  - the old rule;
  - the observed problem;
  - the evidence sample;
  - the proposed new rule;
  - expected benefit;
  - new failure risk;
  - validation plan.
- Do not promote a strategy from research to paper or live from conversation alone. Promotion requires APlan validation artifacts and explicit user approval.

## Safety and integrity rules

- Do not guarantee profit.
- Do not recommend leverage, margin, shorting, or derivatives unless the user explicitly asks and a separate risk framework is defined.
- Do not use future data in backtest reasoning.
- Do not mix horizons: short-term, swing, and medium-term signals need separate rules and metrics.
- Always include transaction costs, slippage, liquidity, suspension/limit-up/limit-down constraints when discussing A 股 execution.
- Prefer fewer, testable rules over many fragile indicators.
