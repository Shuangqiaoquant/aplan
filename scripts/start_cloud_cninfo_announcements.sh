#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-/home/ubuntu/APlan}"
START_DATE="${START_DATE:-20230101}"
END_DATE="${END_DATE:-20260724}"
PID_FILE="$ROOT/state/cninfo_announcements.pid"
LOG_FILE="$ROOT/logs/cninfo_announcements_${START_DATE}_${END_DATE}.log"

cd "$ROOT"
mkdir -p state logs

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Cninfo announcement backfill already running: pid=$pid"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup env PYTHONUNBUFFERED=1 \
  "$ROOT/.venv/bin/aplan-announcements" backfill \
  --root "$ROOT" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --calendar-file "$ROOT/data/processed/trade_calendar.csv" \
  --retries 5 \
  --retry-delay 10 \
  --page-size 100 \
  --request-delay 0.3 \
  --day-delay 0.5 \
  >>"$LOG_FILE" 2>&1 </dev/null &

pid=$!
echo "$pid" >"$PID_FILE"
echo "Cninfo announcement backfill started: pid=$pid"
echo "Log: $LOG_FILE"
