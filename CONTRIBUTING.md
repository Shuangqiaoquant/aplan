# Contributing

Thank you for your interest in APlan. The project is intended to be a careful,
auditable research system rather than a stock-picking channel.

## Good Contributions

- Data quality checks and reproducible data pipelines.
- Backtest realism: trading costs, limits, suspensions, slippage, T+1, and
  point-in-time assumptions.
- Strategy validation that separates training, validation, and paper trading.
- Risk controls, portfolio constraints, and audit records.
- Documentation that makes assumptions, limitations, and failure cases clear.
- Tests for edge cases and regression risks.

## Boundaries

- Do not submit private API keys, broker credentials, personal account data, or
  licensed datasets.
- Do not add language that presents research outputs as investment advice.
- Do not weaken validation, risk, or approval gates to make a strategy look
  better.
- Do not overwrite historical audit records in examples or tests.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

Before opening a pull request, include a short explanation of:

- What changed.
- Why it matters.
- Which tests or reports were run.
- Any remaining limitations.
