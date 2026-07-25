#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-/home/ubuntu/APlan}"
PID_FILE="$ROOT/state/yinhe_fundamentals.pid"

cd "$ROOT"
mkdir -p state logs

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Yinhe fundamentals already running: pid=$pid"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup bash scripts/cloud_yinhe_fundamentals.sh </dev/null >/dev/null 2>&1 &
pid=$!
echo "$pid" > "$PID_FILE"
echo "Yinhe fundamentals started: pid=$pid"
