#!/usr/bin/env bash
set -euo pipefail

ROOT="${APLAN_ROOT:-$HOME/APlan}"
RUN_TESTS="${RUN_TESTS:-1}"

cd "$ROOT"

if [ ! -d ".git" ]; then
  echo "ERROR: $ROOT is not a git checkout. Recreate it with git clone before using cloud_update.sh." >&2
  exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: tracked files are modified on the cloud server." >&2
  echo "Cloud should be a runtime copy. Commit changes from Mac and push to GitHub, then pull here." >&2
  git status --short
  exit 1
fi

git fetch origin
git pull --ff-only

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python3 -m pip install -e .
fi

if [ "$RUN_TESTS" = "1" ]; then
  python3 -m unittest discover -s tests -v
fi

echo "Cloud update complete: $(git rev-parse --short HEAD)"
