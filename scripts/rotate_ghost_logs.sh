#!/bin/bash
# rotate_ghost_logs.sh — bound the growth of the live scanner reject log.
#
# WHY: data/calibration/rejected_candidates.jsonl is appended on EVERY scanner
# rejection (open-per-write O_WRONLY|O_APPEND|O_CREAT, rejected_candidate_log.py:58)
# and — since the 2026-07-13 ghost-settle severance
# (ghost_calibration.auto_settle_enabled: false, verified in config/settings.yaml)
# — is read by NOTHING in the live trading path. Left unbounded it grows ~1GB/mo
# (it hit 1.2G on 2026-07-23). It was rotated by hand on 2026-05-30 and 2026-06-14
# but never scheduled, so it ballooned again. This automates that rotation.
#
# SAFETY: the atomic `mv` is safe because the logger opens the file per-write with
# O_CREAT (rejected_candidate_log.py:57-62) — the next rejection recreates a fresh
# live file. No open handle is orphaned. This ARCHIVES (gzip), never blind-deletes;
# only compressed archives older than PRUNE_DAYS are pruned (a 90-day diagnosis
# window; CLAUDE.md time-filters reject logs to recent anyway).
#
# NOT TOUCHED: rejected_candidates_settled.jsonl (frozen since Jun 22, read
# on-demand by the dashboard/ai_agent as the operator's decision reference, does
# not grow) — intentionally left in place. Only the growing raw log is rotated.
set -euo pipefail

REPO="/Users/mainfolder/Documents/psb-main 1"
CAL="$REPO/data/calibration"
LIVE="$CAL/rejected_candidates.jsonl"
ARCHIVE_DIR="$CAL/archive"
LOGFILE="$CAL/ghost_rotate.log"
THRESHOLD_BYTES=$((200 * 1024 * 1024))   # rotate only when live log exceeds 200 MB
PRUNE_DAYS=90                            # delete compressed archives older than this

mkdir -p "$ARCHIVE_DIR"
ts()  { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts) $*" | tee -a "$LOGFILE"; }

if [ ! -f "$LIVE" ]; then
  log "SKIP: no live file at $LIVE"
  exit 0
fi

SIZE=$(stat -f%z "$LIVE")
SIZE_MB=$((SIZE / 1024 / 1024))
if [ "$SIZE" -lt "$THRESHOLD_BYTES" ]; then
  log "SKIP: live=${SIZE_MB}MB < threshold=$((THRESHOLD_BYTES / 1024 / 1024))MB"
  exit 0
fi

STAMP=$(date "+%Y%m%dT%H%M%SZ")
DEST="$ARCHIVE_DIR/rejected_candidates_archive_${STAMP}.jsonl"
# Atomic rotate — logger's next append recreates $LIVE via O_CREAT.
mv "$LIVE" "$DEST"
log "ROTATED: live=${SIZE_MB}MB -> ${DEST}"
# Drain any in-flight append: the logger opens per-write, so after the mv all NEW
# opens resolve to the fresh $LIVE (O_CREAT); only a write whose open() beat the mv
# can still land in $DEST. That write completes in well under a second — pause so
# gzip compresses a quiescent file and no archive line is lost to the race.
sleep 2
gzip "$DEST"
GZ_MB=$(( $(stat -f%z "${DEST}.gz") / 1024 / 1024 ))
log "COMPRESSED: ${DEST}.gz (${GZ_MB}MB)"

# Bound history: prune only COMPRESSED raw-reject archives older than PRUNE_DAYS.
# Never touches the live log or the frozen settled file.
PRUNED=$(find "$ARCHIVE_DIR" -name 'rejected_candidates_archive_*.jsonl.gz' -type f -mtime +${PRUNE_DAYS} -print -delete 2>/dev/null | wc -l | tr -d ' ')
log "PRUNE: removed ${PRUNED} archive(s) older than ${PRUNE_DAYS}d"
log "DONE"
