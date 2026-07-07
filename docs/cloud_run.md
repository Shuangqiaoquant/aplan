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

## Emergency Rule

If you must hotfix on the cloud:

1. Keep the change minimal.
2. Copy the patch back to the Mac immediately.
3. Commit from the Mac and push to GitHub.
4. Return the cloud checkout to a clean `git pull --ff-only` state.

The cloud server should not become a second independent development branch.
