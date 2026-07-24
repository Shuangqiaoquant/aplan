# APlan Frozen Validation Protocol v1.0

The machine-readable source of truth is `config/validation_protocol.toml`. Its
SHA-256 lock is stored in `config/validation_protocol.lock.json`.

## Frozen Design

- Baseline: point-in-time universe plus adjusted OHLCV only.
- Horizons: 1, 5, 10, 20, 40, and 60 trading days.
- Development: 2023-2024.
- Rolling out-of-sample: 2025.
- Final holdout: 2026 through July 22, opened once.
- Benchmarks: CSI 300, point-in-time industry equal weight, and the same-day
  unadjusted baseline candidate set.
- Metrics: net excess return, median, win rate, drawdown, turnover, costs, and
  return distribution.
- Multiple starts and market-regime slices are mandatory.
- Agent ablations cover market theme, announcement risk, intraday
  confirmation, and portfolio heat.

No horizon is required to work universally. A horizon may be selected only
from the development sample after passing the frozen stability gates. Later
samples accept or reject that choice; they cannot be used to select another
winner.

## Stop Rule

After the frozen sample and cohort thresholds are reached, an Agent without
stable net incremental value is disabled. An Agent with high signal overlap
and no independent increment is merged. No Agent may alter production weights
automatically.

## Change Control

Verify the lock:

```bash
python -m aplan.validation_protocol verify
```

Any methodological change requires a new protocol version, an explicit reason,
and a new lock. Existing reports continue to reference the old protocol hash.
