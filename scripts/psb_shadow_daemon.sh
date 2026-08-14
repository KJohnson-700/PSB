#!/bin/bash
# psb_shadow_daemon.sh — run a one-shot PSB shadow script on a loop, as a nohup daemon.
#
# WHY THIS EXISTS (2026-08-14): five shadows were scheduled as launchd agents
# (com.psb.{dirbreaker-shadow,rotation-shadow,attribution-shadow,floor-release,
# cut-reopen-tripwire}) and had NEVER ONCE EXECUTED. Each had ~1,280 identical
# failure lines and exit code 2:
#
#   /usr/bin/python3: can't open file '.../scripts/<name>.py': [Errno 1] Operation not permitted
#
# TWO separate bugs, both fatal:
#   1. launchd cannot reach ~/Documents under Sequoia TCC. Every PSB helper hits this;
#      the fix is always a nohup daemon from a normal shell, never launchd.
#      (reference_launchd_tcc_blocked_documents_use_nohup_daemon)
#   2. the plists invoked /usr/bin/python3 — the SYSTEM interpreter, which does not
#      have the repo's dependencies. Even with TCC granted they would have failed.
#
# So the shadows silently produced nothing while the roadmap tracked them as
# "accumulating". All five were verified to run clean (exit 0, real output) before
# this wrapper was written — do the same before adding any new one here.
#
# Usage:  nohup bash scripts/psb_shadow_daemon.sh <script-stem> <interval-sec> \
#             > /dev/null 2>&1 < /dev/null &   ; disown
# e.g.    nohup bash scripts/psb_shadow_daemon.sh attribution_shadow 900 ...
#
# Each invocation supervises exactly ONE shadow so a crash-looping script cannot
# take the others down with it. Logs to data/logs/<stem>.daemon.log.
set -uo pipefail

REPO="/Users/mainfolder/Documents/psb-main 1"
STEM="${1:?usage: psb_shadow_daemon.sh <script-stem> <interval-sec>}"
INTERVAL="${2:?usage: psb_shadow_daemon.sh <script-stem> <interval-sec>}"
SCRIPT="$REPO/scripts/$STEM.py"
LOG="$REPO/data/logs/$STEM.daemon.log"
PY="$REPO/.venv/bin/python"

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

if [ ! -f "$SCRIPT" ]; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') FATAL: no script at $SCRIPT" >> "$LOG"; exit 1
fi
if [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') FATAL: no venv python at $PY" >> "$LOG"; exit 1
fi

echo "$(date '+%Y-%m-%dT%H:%M:%S%z') daemon start $STEM (interval=${INTERVAL}s)" >> "$LOG"
FAILS=0
while true; do
  {
    echo "--- $(date '+%Y-%m-%dT%H:%M:%S%z') ---"
    # nice: these are diagnostic, they must never compete with the scan loop.
    nice -n 19 "$PY" "$SCRIPT" 2>&1
    RC=$?
    if [ "$RC" -ne 0 ]; then
      FAILS=$((FAILS + 1))
      echo "!! exit=$RC (consecutive failures: $FAILS)"
    else
      FAILS=0
    fi
    # Loud marker so a silently-dead shadow can never again read as "accumulating".
    if [ "$FAILS" -ge 5 ]; then
      echo "!! $STEM HAS FAILED $FAILS CONSECUTIVE RUNS — treat as BROKEN, not accumulating"
    fi
  } >> "$LOG"
  sleep "$INTERVAL"
done
