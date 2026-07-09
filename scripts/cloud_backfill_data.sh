#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-$HOME/APlan}"
START_DATE="${START_DATE:-20230101}"
END_DATE="${END_DATE:-$(TZ=Asia/Shanghai date +%Y%m%d)}"
DAILY_MAX_DAYS="${DAILY_MAX_DAYS:-20}"
DAILY_DELAY="${DAILY_DELAY:-2.6}"
ADJ_FACTOR_DELAY="${ADJ_FACTOR_DELAY:-65}"
EVIDENCE_MAX_DAYS="${EVIDENCE_MAX_DAYS:-1}"
EVIDENCE_DELAY="${EVIDENCE_DELAY:-3700}"
CALENDAR_MODE="${CALENDAR_MODE:-tushare}"
RUN_AKSHARE="${RUN_AKSHARE:-1}"
AKSHARE_START_YEAR="${AKSHARE_START_YEAR:-2024}"
AKSHARE_SYMBOLS="${AKSHARE_SYMBOLS:-}"
AKSHARE_SYMBOLS_FILE="${AKSHARE_SYMBOLS_FILE:-}"

cd "$ROOT"
mkdir -p logs

if [ ! -d ".venv" ]; then
  echo "ERROR: missing .venv. Run scripts/cloud_bootstrap.sh first." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: missing .env. Fill runtime credentials before backfilling data." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

LOG_PATH="logs/backfill_${START_DATE}_${END_DATE}_$(TZ=Asia/Shanghai date +%Y%m%d%H%M%S).log"
{
  echo "APlan cloud backfill start"
  echo "root=$ROOT"
  echo "start=$START_DATE"
  echo "end=$END_DATE"
  echo "daily_max_days=$DAILY_MAX_DAYS"
  echo "adj_factor_delay=$ADJ_FACTOR_DELAY"
  echo "evidence_max_days=$EVIDENCE_MAX_DAYS"
  echo "git_commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

  echo
  echo "== Tushare daily =="
  aplan-sync backfill \
    --root "$ROOT" \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --datasets daily \
    --calendar-mode "$CALENDAR_MODE" \
    --max-days "$DAILY_MAX_DAYS" \
    --delay "$DAILY_DELAY" \
    --workers 1

  echo
  echo "== Tushare adj_factor =="
  aplan-sync backfill \
    --root "$ROOT" \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --datasets adj_factor \
    --calendar-mode local-daily \
    --max-days "$DAILY_MAX_DAYS" \
    --delay "$ADJ_FACTOR_DELAY" \
    --workers 1

  echo
  echo "== Tushare evidence fields =="
  aplan-sync backfill \
    --root "$ROOT" \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --datasets daily_basic,stk_limit,suspend_d \
    --calendar-mode local-daily \
    --max-days "$EVIDENCE_MAX_DAYS" \
    --delay "$EVIDENCE_DELAY" \
    --workers 1

  if [ "$RUN_AKSHARE" = "1" ]; then
    echo
    echo "== AkShare securities =="
    aplan-akshare securities --root "$ROOT" --as-of "$(TZ=Asia/Shanghai date +%Y-%m-%d)" --retries 2 --retry-delay 2

    echo
    echo "== AkShare spot valuations =="
    aplan-akshare spot-valuations --root "$ROOT" --date "$END_DATE" --retries 2 --retry-delay 2

    if [ -n "$AKSHARE_SYMBOLS" ] || [ -n "$AKSHARE_SYMBOLS_FILE" ]; then
      echo
      echo "== AkShare financial indicators =="
      args=(financial-indicators --root "$ROOT" --as-of "$(TZ=Asia/Shanghai date +%Y-%m-%d)" --start-year "$AKSHARE_START_YEAR" --retries 2 --retry-delay 2)
      if [ -n "$AKSHARE_SYMBOLS" ]; then
        args+=(--symbols "$AKSHARE_SYMBOLS")
      fi
      if [ -n "$AKSHARE_SYMBOLS_FILE" ]; then
        args+=(--symbols-file "$AKSHARE_SYMBOLS_FILE")
      fi
      aplan-akshare "${args[@]}"
    fi
  fi

  echo
  echo "== Evidence coverage =="
  aplan-evidence-coverage --root "$ROOT"

  echo "APlan cloud backfill complete"
} 2>&1 | tee "$LOG_PATH"

echo "Backfill log: $LOG_PATH"
