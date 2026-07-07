#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-$HOME/APlan}"
TRADE_DATE="${TRADE_DATE:-$(TZ=Asia/Shanghai date +%Y%m%d)}"
AUTO_PULL="${APLAN_AUTO_PULL:-0}"

cd "$ROOT"
mkdir -p logs

if [ "$AUTO_PULL" = "1" ]; then
  RUN_TESTS="${RUN_TESTS:-0}" "$ROOT/scripts/cloud_update.sh"
fi

if [ ! -d ".venv" ]; then
  echo "ERROR: missing .venv. Run scripts/cloud_bootstrap.sh first." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: missing .env. Copy .env.example to .env and fill credentials." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

LOG_PATH="logs/daily_${TRADE_DATE}_$(TZ=Asia/Shanghai date +%H%M%S).log"
{
  echo "APlan cloud daily start"
  echo "root=$ROOT"
  echo "trade_date=$TRADE_DATE"
  echo "git_commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  aplan-daily --root "$ROOT" --date "$TRADE_DATE"
  echo "APlan cloud daily complete"
} 2>&1 | tee "$LOG_PATH"

echo "Daily log: $LOG_PATH"
