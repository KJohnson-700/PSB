#!/bin/bash
# bnb_a1hsl_tripwire_daemon.sh — run the bnb alt_1h_simple_long tripwire on a loop.
#
# launchd CANNOT reach ~/Documents on Sequoia (TCC), so PSB helpers run as nohup
# daemons from a normal shell instead. Start with:
#   nohup bash scripts/bnb_a1hsl_tripwire_daemon.sh > /dev/null 2>&1 < /dev/null &
#   disown
#
# The tripwire itself is idempotent and self-limiting: once it cuts, it records
# cut=true in data/runtime/bnb_a1hsl_tripwire.json and every later run no-ops.
set -uo pipefail

REPO="/Users/mainfolder/Documents/psb-main 1"
LOG="$REPO/data/logs/bnb_a1hsl_tripwire.log"
INTERVAL=900   # 15 min — the lane closes at most a few 1h trades an hour

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

echo "$(date '+%Y-%m-%dT%H:%M:%S%z') daemon start (interval=${INTERVAL}s)" >> "$LOG"
while true; do
  {
    echo "--- $(date '+%Y-%m-%dT%H:%M:%S%z') ---"
    "$REPO/.venv/bin/python" "$REPO/scripts/bnb_a1hsl_tripwire.py" 2>&1
  } >> "$LOG"
  sleep "$INTERVAL"
done
