#!/bin/bash
# psb_tooling_daemon.sh — keep the read-only data pipelines alive.
#
# WHY THIS EXISTS (2026-08-17)
# ───────────────────────────
# Two pipelines were dead and neither announced it:
#
#   1. GHOST ROTATION. `com.psb.ghost-rotate` is a launchd agent, and launchd
#      cannot touch ~/Documents on Sequoia (TCC). Every run died with
#      "Operation not permitted" on the `mv`, so the agent sat at exit status 1
#      while the reject log grew to 1.3GB. The successful rotations in
#      ghost_rotate.log (through 08-14) were HAND runs, not the schedule.
#      Five other psb agents were already renamed .DISABLED_tcc_20260814 for
#      exactly this reason — the rotate agent was missed.
#
#   2. STOPPED-TRADE SETTLER. `settle_stopped_trades.py --loop` was started by
#      hand and died with the session that launched it. Nothing re-armed it, so
#      stopped_trades_settled.jsonl froze. That ledger is the ONLY thing that
#      tells a stopped loser apart from a stopped WINNER, and it is LIVE REALIZED
#      data — the only class of evidence decisions are made on.
#
# A nohup daemon inherits the launching shell's TCC grant, so it CAN write
# ~/Documents. That is the documented workaround and this is what it looks like.
#
# WHAT IT DOES NOT DO
# ───────────────────
# Read-only on bot data. Never restarts the bot. Never edits config. The rotate
# script it calls is atomic (mv + O_CREAT recreate) and defers its own heavy
# settled-archive step while a bot is trading.
#
# It also does NOT resurrect ghost settlement. `ghost_calibration.auto_settle_enabled`
# is false by deliberate 07-13 severance and ghost data is not a decision input.
# rejected_candidates_settled.jsonl being static is CORRECT, not stale.
#
# USAGE
#   nohup scripts/psb_tooling_daemon.sh > /dev/null 2>&1 < /dev/null & disown
#   scripts/psb_tooling_daemon.sh --status
#   scripts/psb_tooling_daemon.sh --stop
set -uo pipefail

REPO="/Users/mainfolder/Documents/psb-main 1"
LOG="$REPO/data/calibration/psb_tooling_daemon.log"
PIDFILE="$REPO/data/calibration/psb_tooling_daemon.pid"
VENV_PY="$REPO/.venv/bin/python"

ROTATE_SH="$REPO/scripts/rotate_ghost_logs.sh"
SETTLER="$REPO/scripts/settle_stopped_trades.py"
BAND_GUARD="$REPO/scripts/blocked_band_guard.py"

SETTLE_EVERY_SEC=1800      # 30 min — the settler is idempotent and cheap
GUARD_EVERY_SEC=3600       # 60 min — the release guards hit the GAMMA API, don't spam it
ROTATE_AT_HOUR=4           # 04:15 local, matching the schedule the dead agent had
ROTATE_AT_MIN=15
TICK_SEC=60

ts()  { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts) $*" >> "$LOG"; }

case "${1:-}" in
  --status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "RUNNING pid=$(cat "$PIDFILE")"
    else
      echo "NOT RUNNING"
    fi
    echo "--- last 12 log lines ---"
    tail -12 "$LOG" 2>/dev/null || echo "(no log yet)"
    exit 0
    ;;
  --stop)
    # 2026-08-17: WAIT for the exit. The tick loop sits in `sleep $TICK_SEC`, so TERM is
    # not handled until that sleep returns — up to a minute later. A stop-then-start that
    # did not wait raced: the OLD instance's trap fired 12s AFTER the new one had started
    # and deleted the NEW instance's pidfile, leaving a live daemon that --status and the
    # probation row both reported as DOWN. Never return from --stop before it is gone.
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      _p="$(cat "$PIDFILE")"
      kill -TERM "$_p"
      for _i in $(seq 1 "$((TICK_SEC + 15))"); do
        kill -0 "$_p" 2>/dev/null || break
        sleep 1
      done
      if kill -0 "$_p" 2>/dev/null; then
        echo "pid=$_p did not exit on TERM; sending KILL"
        kill -KILL "$_p" 2>/dev/null
        sleep 1
      fi
      # only clear the pidfile if it is still OURS
      [ -f "$PIDFILE" ] && [ "$(cat "$PIDFILE")" = "$_p" ] && rm -f "$PIDFILE"
      echo "stopped pid=$_p"
    else
      echo "NOT RUNNING"
    fi
    exit 0
    ;;
esac

# Single instance. A second copy would double-run the rotate and race the mv.
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  log "REFUSING START: already running pid=$(cat "$PIDFILE")"
  echo "already running pid=$(cat "$PIDFILE")" >&2
  exit 1
fi
echo $$ > "$PIDFILE"
# ⛔ Only remove the pidfile if it still names US. A late-firing trap from a previous
# instance was deleting a NEWER instance's pidfile (see --stop above) — the daemon stayed
# alive while every health check reported it DOWN. Guard the teardown, not just the start.
trap '
  log "STOP: pid=$$"
  if [ -f "$PIDFILE" ] && [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ]; then
    rm -f "$PIDFILE"
  else
    log "STOP: pidfile now owned by another instance — leaving it alone"
  fi
  exit 0' TERM INT

log "START: pid=$$ settle_every=${SETTLE_EVERY_SEC}s rotate_at=${ROTATE_AT_HOUR}:$(printf '%02d' $ROTATE_AT_MIN)"

# Prove TCC on the way up rather than discovering it silently on the first run —
# this is the exact failure that hid for three days.
if ! touch "$REPO/data/calibration/.tooling_daemon_write_probe" 2>/dev/null; then
  log "FATAL: cannot write $REPO/data/calibration — no TCC grant. Relaunch from a shell that has Full Disk Access."
  rm -f "$PIDFILE"
  exit 1
fi
rm -f "$REPO/data/calibration/.tooling_daemon_write_probe"
log "TCC PROBE: ok — can write data/calibration"

LAST_SETTLE=0
LAST_GUARD=0
LAST_ROTATE_DAY=""

while true; do
  NOW=$(date +%s)

  # ── stopped-trade settler ──────────────────────────────────────────────────
  if [ $((NOW - LAST_SETTLE)) -ge "$SETTLE_EVERY_SEC" ]; then
    if [ -x "$VENV_PY" ] && [ -f "$SETTLER" ]; then
      OUT=$(nice -n 19 "$VENV_PY" "$SETTLER" --once 2>&1 | tail -3)
      if [ -n "$OUT" ]; then
        log "SETTLER: $(echo "$OUT" | tr '\n' ' | ')"
      else
        log "SETTLER: ran, no output"
      fi
    else
      log "SETTLER SKIP: venv python or script missing"
    fi
    LAST_SETTLE=$NOW

    # ── bucket-A settle + est_prob calibration (Step 2 Phase A, operator GO) ──
    # Same cadence as the settler. entry_exit_split OWNS exit_layer_settled.jsonl
    # (one writer per file); est_calibration_report only READS it and refits
    # data/calibration/est_prob_calibration.json. Consumer is SIZING ONLY — this
    # cannot change bot behavior; it is the measurement layer for the Kelly flip.
    if [ -x "$VENV_PY" ] && [ -f "$REPO/scripts/entry_exit_split.py" ]; then
      SOUT=$(nice -n 19 "$VENV_PY" "$REPO/scripts/entry_exit_split.py" \
                 --since 2026-08-16T22:57:15 --settle --limit 150 --min-n 999 2>&1 \
                 | grep -E "settled|counterfactual" | tail -1)
      log "SPLIT-SETTLE: ${SOUT:-no output}"
    fi
    if [ -x "$VENV_PY" ] && [ -f "$REPO/scripts/est_calibration_report.py" ]; then
      nice -n 19 "$VENV_PY" "$REPO/scripts/est_calibration_report.py" \
          --write-state --quiet
      EC_RC=$?
      if [ "$EC_RC" -eq 0 ]; then
        log "EST-CAL: state refreshed"
      else
        log "EST-CAL: rc=$EC_RC (3 = coverage <50%, fits untrustworthy — settle is behind)"
      fi
    fi
  fi

  # ── release guards (cut-reopen + rsi-floor) ────────────────────────────────
  # These replace the TCC-dead cut_reopen_tripwire / floor_release_monitor agents,
  # retargeted at the LIVE gate reasons. FLAG-only: they never write config.
  if [ $((NOW - LAST_GUARD)) -ge "$GUARD_EVERY_SEC" ]; then
    if [ -x "$VENV_PY" ] && [ -f "$BAND_GUARD" ]; then
      # --include-archive: rotation moves history OUT of the live log, so without it the
      # first day after every rotate reads n=2 and says "gate is quiet". The guard's own
      # --hours window still applies to archive rows, so this does NOT reopen era pooling.
      GOUT=$(nice -n 19 "$VENV_PY" "$BAND_GUARD" --guard both --include-archive \
                 --limit 250 --throttle 0.05 2>&1)
      # Log the verdict lines and any FLAG, not the whole table.
      echo "$GOUT" | grep -E "blocked_band_guard\[|FLAG|⚠️" | while read -r L; do
        log "GUARD: $L"
      done
      if echo "$GOUT" | grep -q "FLAG_RELEASE_REVIEW"; then
        log "GUARD: ⚠️ release review flagged — a gate is blocking a +EV band. HUMAN decision."
      fi
    else
      log "GUARD SKIP: venv python or blocked_band_guard.py missing"
    fi
    LAST_GUARD=$NOW
  fi

  # ── ghost log rotation, once per day at ROTATE_AT ──────────────────────────
  TODAY=$(date "+%Y-%m-%d")
  H=$(date +%-H)
  M=$(date +%-M)
  if [ "$TODAY" != "$LAST_ROTATE_DAY" ] && [ "$H" -eq "$ROTATE_AT_HOUR" ] && [ "$M" -ge "$ROTATE_AT_MIN" ]; then
    log "ROTATE: starting (live=$( [ -f "$REPO/data/calibration/rejected_candidates.jsonl" ] && echo "$(( $(stat -f%z "$REPO/data/calibration/rejected_candidates.jsonl") / 1024 / 1024 ))MB" || echo "absent" ))"
    if bash "$ROTATE_SH" >> "$LOG" 2>&1; then
      log "ROTATE: exit 0"
    else
      log "ROTATE: FAILED exit=$? — see above"
    fi
    LAST_ROTATE_DAY="$TODAY"
  fi

  sleep "$TICK_SEC"
done
