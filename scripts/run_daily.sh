#!/usr/bin/env bash
# Daily orchestrator wrapper. Invoked by launchd at 09:00 (or manually / by Shortcuts.app).
# Redirects all output to logs/run_daily.log so pre-Python crashes are captured too.

set -uo pipefail

# Resolve the repo root from this script's own location, so the wrapper is
# portable regardless of where the clone lives.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

mkdir -p logs

# shellcheck disable=SC1091
source .venv/bin/activate

python scripts/run_daily.py >> logs/run_daily.log 2>&1
exit $?
