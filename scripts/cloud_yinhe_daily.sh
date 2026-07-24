#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-$HOME/APlan}"
LOOKBACK_DAYS="${YINHE_LOOKBACK_DAYS:-14}"
END_DATE="${YINHE_END_DATE:-$(TZ=Asia/Shanghai date +%Y%m%d)}"
START_DATE="${YINHE_START_DATE:-$(TZ=Asia/Shanghai date -d "$LOOKBACK_DAYS days ago" +%Y%m%d)}"
MAX_DAYS="${YINHE_MAX_DAYS:-1}"
DELAY="${YINHE_DELAY:-0}"
REFRESH_SECURITIES="${YINHE_REFRESH_SECURITIES:-0}"
SYMBOLS_FILE="${YINHE_SYMBOLS_FILE:-data/processed/yinhe_symbols.txt}"

cd "$ROOT"
mkdir -p logs

if [ ! -d ".venv" ]; then
  echo "ERROR: missing .venv. Run scripts/cloud_bootstrap.sh first." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: missing .env. Fill Yinhe runtime credentials before syncing data." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

LOG_PATH="logs/yinhe_daily_${END_DATE}_$(TZ=Asia/Shanghai date +%H%M%S).log"
{
  echo "APlan Yinhe daily start"
  echo "root=$ROOT"
  echo "start=$START_DATE"
  echo "end=$END_DATE"
  echo "max_days=$MAX_DAYS"
  echo "git_commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

  if [ "$REFRESH_SECURITIES" = "1" ] || [ ! -f "data/processed/yinhe_securities.csv" ]; then
    echo
    echo "== Yinhe securities =="
    aplan-yinhe securities --root "$ROOT" --as-of "$(TZ=Asia/Shanghai date +%Y-%m-%d)"
  fi

  echo
  echo "== Yinhe A-share pool =="
  aplan-yinhe build-symbols --root "$ROOT" --output "$SYMBOLS_FILE"

  echo
  echo "== Yinhe daily backfill =="
  aplan-yinhe backfill-daily \
    --root "$ROOT" \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --symbols-file "$SYMBOLS_FILE" \
    --max-days "$MAX_DAYS" \
    --delay "$DELAY"

  echo "APlan Yinhe daily complete"
} 2>&1 | tee "$LOG_PATH"

echo "Yinhe daily log: $LOG_PATH"
