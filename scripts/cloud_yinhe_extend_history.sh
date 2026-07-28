#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/home/ubuntu/APlan}"
START_DATE="${START_DATE:-20210101}"
END_DATE="${END_DATE:-20260727}"
POOL_FILE="${POOL_FILE:-data/processed/yinhe_symbols_${START_DATE}_${END_DATE}.txt}"
CHUNK_SIZE="${CHUNK_SIZE:-50}"
QUERY_RETRIES="${QUERY_RETRIES:-5}"
RETRY_DELAY="${RETRY_DELAY:-10}"

cd "$ROOT"
mkdir -p logs reports/yinhe_history_extension
source .venv/bin/activate

python - "$ROOT" "$START_DATE" "$END_DATE" <<'PY'
import json
import sys
from pathlib import Path

root, start, end = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
path = root / "data/processed/security_history/manifest.json"
manifest = json.loads(path.read_text(encoding="utf-8"))
ready = (
    manifest.get("status") == "validated"
    and manifest.get("point_in_time") is True
    and str(manifest.get("coverage_start") or "") <= start
    and str(manifest.get("coverage_end") or "") >= end
)
if not ready:
    raise SystemExit(f"historical security state is not ready: {path}")
print(
    "historical security state ready:",
    manifest["coverage_start"],
    manifest["coverage_end"],
    manifest["security_count"],
)
PY

echo "[$(date --iso-8601=seconds)] build historical PIT symbol pool"
python -m aplan.yinhe_history_extension build-pool \
  --root "$ROOT" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --output "$ROOT/$POOL_FILE"

for period in 20210101:20211231 20220101:20221231; do
  period_start="${period%:*}"
  period_end="${period#*:}"
  echo "[$(date --iso-8601=seconds)] backfill raw ${period_start}-${period_end}"
  aplan-yinhe backfill-range \
    --start "$period_start" \
    --end "$period_end" \
    --symbols-file "$POOL_FILE" \
    --chunk-size "$CHUNK_SIZE" \
    --query-retries "$QUERY_RETRIES" \
    --retry-delay "$RETRY_DELAY"
done

echo "[$(date --iso-8601=seconds)] rebuild one continuous qfq layer"
aplan-yinhe adjustment-ad \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --symbols-file "$POOL_FILE" \
  --chunk-size "$CHUNK_SIZE"

echo "[$(date --iso-8601=seconds)] run standard acceptance"
aplan-yinhe acceptance \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --calendar-file data/processed/trade_calendar.csv

echo "[$(date --iso-8601=seconds)] run history extension acceptance"
python -m aplan.yinhe_history_extension audit \
  --root "$ROOT" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --output "$ROOT/reports/yinhe_history_extension"

echo "[$(date --iso-8601=seconds)] completed"
