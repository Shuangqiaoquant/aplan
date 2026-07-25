#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-/home/ubuntu/APlan}"
cd "$ROOT"
mkdir -p logs state

ranges=(
  "20230101:20231231"
  "20240101:20241231"
  "20250101:20251231"
  "20260101:20260724"
)
pids=()

run_range() {
  local start="$1"
  local end="$2"
  local log="$3"
  local attempt=0
  while [[ "$attempt" -lt 20 ]]; do
    if env PYTHONUNBUFFERED=1 \
      "$ROOT/.venv/bin/aplan-announcements" backfill \
      --root "$ROOT" \
      --start "$start" \
      --end "$end" \
      --calendar-file "$ROOT/data/processed/trade_calendar.csv" \
      --retries 5 \
      --retry-delay 10 \
      --page-size 30 \
      --request-delay 0.3 \
      --day-delay 0.5 \
      --skip-archive \
      >>"$log" 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    echo "[$(date -Is)] worker ${start}-${end} restart ${attempt}/20" >>"$log"
    sleep 30
  done
  return 1
}

for period in "${ranges[@]}"; do
  start="${period%:*}"
  end="${period#*:}"
  log="$ROOT/logs/cninfo_announcements_${start}_${end}.log"
  run_range "$start" "$end" "$log" &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    failed=1
  fi
done
if [[ "$failed" -ne 0 ]]; then
  echo "At least one Cninfo announcement worker failed"
  exit 1
fi

"$ROOT/.venv/bin/aplan-announcements" build-archive \
  --root "$ROOT" \
  --start 20230101 \
  --end 20260724 \
  --calendar-file "$ROOT/data/processed/trade_calendar.csv"
