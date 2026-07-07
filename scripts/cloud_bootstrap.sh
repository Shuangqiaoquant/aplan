#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-$HOME/APlan}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT"

if [ ! -d ".git" ]; then
  echo "ERROR: $ROOT is not a git checkout. Clone from GitHub first, then rerun this script." >&2
  exit 1
fi

mkdir -p data/raw data/processed reports runs state logs

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

if [ -f "data/raw/yinhe/星耀数智/tgw-1.0.8.7-py3-none-any.whl" ]; then
  python3 -m pip install "data/raw/yinhe/星耀数智/tgw-1.0.8.7-py3-none-any.whl"
fi

if [ -f "data/raw/yinhe/星耀数智/AmazingData/AmazingData-1.1.7-cp312-none-any.whl" ]; then
  python3 -m pip install "data/raw/yinhe/星耀数智/AmazingData/AmazingData-1.1.7-cp312-none-any.whl"
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created $ROOT/.env from .env.example. Fill it before running live sync jobs."
fi

python3 -m unittest discover -s tests -v

echo "Cloud bootstrap complete: $ROOT"
