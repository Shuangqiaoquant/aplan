#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-/home/ubuntu/APlan}"
PID_FILE="$ROOT/state/cninfo_announcements_parallel.pid"
LOG_FILE="$ROOT/logs/cninfo_announcements_parallel_driver.log"

cd "$ROOT"
mkdir -p state logs

if pgrep -f "aplan-announcements backfill" >/dev/null 2>&1; then
  echo "A Cninfo announcement backfill is already running"
  exit 0
fi
if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Parallel Cninfo announcement driver already running: pid=$pid"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup bash "$ROOT/scripts/cloud_cninfo_announcements_parallel.sh" \
  >>"$LOG_FILE" 2>&1 </dev/null &
pid=$!
echo "$pid" >"$PID_FILE"
echo "Parallel Cninfo announcement backfill started: pid=$pid"
echo "Driver log: $LOG_FILE"
