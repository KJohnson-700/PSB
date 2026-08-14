#!/usr/bin/env bash
# Adaptive-sizer state refresh daemon (out-of-process, like the ghost settler /
# exit shadow daemon). The bot's IN-PROCESS recompute (_maybe_recompute_adaptive_sizer)
# only fires when a single exit-settle run resolves >= recompute_min_new_settles trades;
# at this low paper frequency settles trickle 1-3/run so it almost never fires and the
# LIVE sizer state (data/calibration/adaptive_sizer_state.json) went 4 DAYS stale (2026-08-03).
# This loop recomputes it every INTERVAL seconds from trades_settled.jsonl so the live
# per-lane multipliers stay current. Read-only w.r.t. the bot: it only writes the state
# file the sizer already reads (resolve_size_mult, mtime-cached). NEVER touches positions.
#
# Run (nohup; launchd can't touch ~/Documents under Sequoia TCC):
#   nohup bash scripts/adaptive_sizer_refresh_daemon.sh >> data/calibration/adaptive_sizer_refresh.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
INTERVAL="${1:-600}"
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
echo "[sizer-refresh] start interval=${INTERVAL}s py=$PY $(date -u +%FT%TZ)"
while true; do
  "$PY" -m src.analysis.adaptive_lane_sizer 2>&1 | sed "s/^/[sizer-refresh $(date -u +%FT%TZ)] /"
  sleep "$INTERVAL"
done
