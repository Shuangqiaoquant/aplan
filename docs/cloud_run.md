# APlan Cloud Runtime

This document fixes the working relationship between GitHub, the local Mac, and the Tencent Cloud server.

## Roles

```text
GitHub     = source of truth for code
Mac local  = primary development and debugging workspace
Cloud VM   = long-running runtime for sync jobs, reports, logs, and data
```

Do not edit tracked code on the cloud server during normal work. Change code on the Mac, commit it, push it to GitHub, then pull it on the cloud server.

## What Belongs Where

GitHub:

- source code under `src/`
- tests under `tests/`
- docs, README, templates, scripts
- small non-secret examples only

Mac:

- normal code development
- fast local tests
- small sample data for debugging
- Git commits and pushes

Cloud VM:

- `.env` with production/runtime credentials
- `data/raw/` and `data/processed/`
- `reports/`, `runs/`, `state/`, `logs/`
- cron or systemd timers

Never commit `.env`, raw licensed data, broker credentials, or large generated datasets.

## First-Time Cloud Setup

If `~/APlan` was copied manually before, keep its runtime assets before switching to GitHub:

```bash
cd ~
mv APlan APlan.manual.$(date +%Y%m%d%H%M%S)
git clone git@github.com:shuangqiaoquant/aplan.git APlan
cp APlan.manual.*/.env APlan/.env
cp -a APlan.manual.*/data/raw/* APlan/data/raw/ 2>/dev/null || true
```

Then bootstrap the cloud checkout:

```bash
cd ~/APlan
bash scripts/cloud_bootstrap.sh
```

If the repository is private, configure an SSH key on the cloud server and add the public key to GitHub before `git clone`.

## Daily Cloud Update

After developing on the Mac:

```bash
git add .
git commit -m "Describe the change"
git push
```

Then on the cloud server:

```bash
cd ~/APlan
bash scripts/cloud_update.sh
```

`cloud_update.sh` refuses to pull if tracked files were edited on the cloud. This protects the “Mac develops, cloud runs” rule.

## Daily Runtime Job

Manual run:

```bash
cd ~/APlan
TRADE_DATE=20260706 bash scripts/cloud_daily.sh
```

Default date is the current `Asia/Shanghai` date:

```bash
bash scripts/cloud_daily.sh
```

Logs are written to:

```text
logs/daily_<YYYYMMDD>_<HHMMSS>.log
```

## Data Backfill Job

Use the backfill job to turn the runtime database from “can run” into “can optimize”.
It fills data in conservative batches so low-frequency API accounts can resume safely.

Default run:

```bash
cd ~/APlan
bash scripts/cloud_backfill_data.sh
```

Useful controls:

```bash
START_DATE=20230101 \
END_DATE=20260708 \
DAILY_MAX_DAYS=20 \
EVIDENCE_MAX_DAYS=1 \
bash scripts/cloud_backfill_data.sh
```

The script runs these stages:

- Tushare `daily` for up to `DAILY_MAX_DAYS` missing weekdays. The default avoids `trade_cal`
  because some Tushare accounts limit that endpoint to once per hour.
- Tushare `adj_factor` for up to `DAILY_MAX_DAYS` local daily dates. `ADJ_FACTOR_DELAY`
  defaults to `65` seconds for accounts limited to about once per minute.
- Tushare `daily_basic,stk_limit,suspend_d` for up to `EVIDENCE_MAX_DAYS` local daily dates.
- AkShare securities and spot valuations for the end date.

## Yinhe Historical Range Bootstrap

`backfill-daily` queries the whole symbol pool once for every date. Use it for the normal
daily update. For an initial historical window, `backfill-range` is more efficient: it
queries each symbol once for the whole range, then splits returned K-lines into the same
per-date processed files.

Always validate the supplier response with a small symbol sample first:

```bash
aplan-yinhe backfill-range \
  --start 20260601 \
  --end 20260722 \
  --symbols 600000,000001,600519,300750,000333
```

Check that `returned_dates`, `daily_rows`, and the generated CSV files are reasonable
before expanding the symbol list. Existing date files are preserved unless `--overwrite`
is passed.

For a large pool, range queries run in chunks of 250 symbols by default. Each completed
chunk is merged into the per-date CSV files and recorded below
`data/raw/yinhe/ranges/`. Re-running the same command resumes from those checkpoints.
Use a smaller `--chunk-size` on a low-memory server. Do not pass `--overwrite` when
resuming, because it intentionally ignores checkpoints and re-queries every chunk.
Transient supplier timeouts are retried three times by default with increasing waits.
Tune this with `--query-retries` and `--retry-delay` when the upstream service is unstable.
- Optional AkShare financial indicators for a provided symbol list.
- Evidence coverage report.

`EVIDENCE_MAX_DAYS` defaults to `1` and `EVIDENCE_DELAY` defaults to `3700` seconds because some Tushare accounts limit `daily_basic` to about once per hour. Repeat the same command; it resumes from missing dates.

To enrich a small candidate pool with AkShare fundamentals:

```bash
AKSHARE_SYMBOLS=600000,000001,300750 \
AKSHARE_START_YEAR=2024 \
bash scripts/cloud_backfill_data.sh
```

Or use a file:

```bash
AKSHARE_SYMBOLS_FILE=data/processed/candidates/latest_symbols.txt \
bash scripts/cloud_backfill_data.sh
```

## Cron Example

Open crontab:

```bash
crontab -e
```

Run after China A-share close on weekdays:

```cron
30 16 * * 1-5 APLAN_ROOT=/home/ubuntu/APlan /bin/bash /home/ubuntu/APlan/scripts/cloud_daily.sh >> /home/ubuntu/APlan/logs/cron.log 2>&1
```

If you want the cloud server to pull the latest GitHub code before each job:

```cron
30 16 * * 1-5 APLAN_ROOT=/home/ubuntu/APlan APLAN_AUTO_PULL=1 /bin/bash /home/ubuntu/APlan/scripts/cloud_daily.sh >> /home/ubuntu/APlan/logs/cron.log 2>&1
```

Use auto-pull only after the GitHub workflow is stable.

Run the backfill job once per night while data is incomplete:

```cron
10 2 * * 2-6 APLAN_ROOT=/home/ubuntu/APlan DAILY_MAX_DAYS=20 EVIDENCE_MAX_DAYS=1 /bin/bash /home/ubuntu/APlan/scripts/cloud_backfill_data.sh >> /home/ubuntu/APlan/logs/cron_backfill.log 2>&1
```

When Yinhe daily data is enabled, first test one manual run:

```bash
cd /home/ubuntu/APlan
source .venv/bin/activate
bash scripts/cloud_yinhe_daily.sh
```

The script builds a Shanghai/Shenzhen A-share pool from `yinhe_securities.csv`, excludes ST and
delisting-risk names, looks back 14 calendar days, and fills at most one missing weekday per run.
After the manual run succeeds, schedule it after the market close:

```cron
45 17 * * 1-5 APLAN_ROOT=/home/ubuntu/APlan YINHE_MAX_DAYS=1 /bin/bash /home/ubuntu/APlan/scripts/cloud_yinhe_daily.sh >> /home/ubuntu/APlan/logs/cron_yinhe.log 2>&1
```

Set `YINHE_REFRESH_SECURITIES=1` for an occasional manual run to refresh the security list. Files
created earlier with only sample symbols should be rebuilt once with `backfill-daily --overwrite`
before treating them as full-market history.

## Emergency Rule

If you must hotfix on the cloud:

1. Keep the change minimal.
2. Copy the patch back to the Mac immediately.
3. Commit from the Mac and push to GitHub.
4. Return the cloud checkout to a clean `git pull --ff-only` state.

The cloud server should not become a second independent development branch.
