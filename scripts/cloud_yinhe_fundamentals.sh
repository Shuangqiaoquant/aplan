#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-/home/ubuntu/APlan}"
START_DATE="${START_DATE:-20220101}"
END_DATE="${END_DATE:-20260722}"
CHUNK_SIZE="${CHUNK_SIZE:-50}"
SYMBOLS_FILE="${SYMBOLS_FILE:-data/processed/security_history/security_master.csv}"
LOG_FILE="$ROOT/logs/yinhe_fundamentals_${START_DATE}_${END_DATE}.log"
PID_FILE="$ROOT/state/yinhe_fundamentals.pid"

cd "$ROOT"
mkdir -p logs state
trap 'rm -f "$PID_FILE"' EXIT
exec > >(
  sed -u \
    -e '/^TGW Logon information:/d' \
    -e '/^logon json/d' \
    >> "$LOG_FILE"
) 2>&1

echo "[$(date -Is)] Yinhe point-in-time fundamentals start"
echo "root=$ROOT"
echo "range=$START_DATE-$END_DATE"
echo "symbols_file=$SYMBOLS_FILE"
echo "chunk_size=$CHUNK_SIZE"

source .venv/bin/activate
PYTHONUNBUFFERED=1 aplan-yinhe fundamentals-ad \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --symbols-file "$SYMBOLS_FILE" \
  --chunk-size "$CHUNK_SIZE"

echo "[$(date -Is)] Yinhe point-in-time fundamentals complete"
