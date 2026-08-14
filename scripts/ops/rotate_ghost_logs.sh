# ====================================================================
# MIRROR COPY — version control only. The LIVE script that launchd
# (com.psb.ghost-rotate, daily 04:15) actually executes is:
#     ~/.hermes/bin/rotate_ghost_logs.sh
# Editing THIS file changes nothing at runtime. Edit the live one, then
# re-copy here so the logic survives a machine rebuild.
# Synced: 2026-08-14 13:34 -0700
# ====================================================================

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
# SETTLED LOG (added 2026-08-13): the claim below — that
# rejected_candidates_settled.jsonl is "frozen since Jun 22 ... does not grow" —
# WAS FALSE. Measured 2026-08-13: it spans 2026-07-31 -> 2026-08-14, 812,267 rows,
# 1.4GB, growing ~100MB/day, and its settled_index sidecar is 1,028,876 ids which
# the bot holds RESIDENT as a 141MB Python set. That footprint was a direct
# contributor to the bot's ~1GB RSS and the box freezing. dashboard/server.py:916
# already documents this file pinning a CPU core (cycles 6s -> 300s+) back when it
# was only 260MB. It is now 5.4x that. So it IS rotated now — via the purpose-built
# step-3c archiver, which never archives an UNSETTLED ghost and keeps every
# ghost_id in the index, so the settle loop stays closed.
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

# ─────────────────────────────────────────────────────────────────────────────
# STEP 0a — GHOST -> DUCKDB INGEST. Runs FIRST, before anything archives or
# truncates the settled jsonl, so no row can be trimmed before it is warehoused.
#
# WHY (2026-08-14): the warehouse had stopped loading on 2026-06-17, so ~2 months
# of ghosts existed only as a 1.4GB jsonl that every analysis re-scanned end to
# end. In DuckDB the same history is 84MB and a dedupe-by-market query is instant
# instead of a multi-minute file walk. The warehouse also makes the settled
# archive below SAFE: history stays queryable after the jsonl is trimmed.
#
# --live-only reads just the live jsonl. The static .gz archives are already
# ingested (full backfill ran 2026-08-14), and re-reading them nightly was the
# known lock-hog. Ingest is idempotent — it anti-joins on ghost_id — so a missed
# night self-heals on the next run.
#
# NOT fatal: if this fails we log and CONTINUE to the archive/rotate steps, but
# we do NOT let a failed ingest silently precede an archive — see the guard.
DUCK_LOADER="$REPO/scripts/psb_ghost_duckdb.py"
INGEST_OK=0
if [ ! -x "$REPO/.venv/bin/python" ] || [ ! -f "$DUCK_LOADER" ]; then
  log "DUCKDB SKIP: loader or venv python missing"
else
  if nice -n 19 "$REPO/.venv/bin/python" "$DUCK_LOADER" --ingest --live-only >>"$LOGFILE" 2>&1; then
    INGEST_OK=1
    log "DUCKDB INGEST: ok ($( [ -f "$CAL/ghost.duckdb" ] && echo "$(( $(stat -f%z "$CAL/ghost.duckdb") / 1024 / 1024 ))MB" || echo "?" ))"
  else
    log "DUCKDB INGEST: FAILED — settled archive will be SKIPPED this run to avoid trimming un-warehoused rows"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 0 — SETTLED ghost log (must run BEFORE the raw-log early-exits below, or
# it would never execute on a night when the raw log is under threshold).
#
# LIVE-SESSION GUARD: archiving rewrites the settled jsonl, which changes its
# inode, which forces ghost_calibration._rebuild_settled_index() to re-scan the
# whole file + archive shards. That is a heavy one-shot parse and it is exactly
# the thing that stalled the scan loop before (6s -> 300s+ cycles). So we DEFER
# while a bot is trading and retry the next night. Force with
# PSB_FORCE_SETTLED_ARCHIVE=1 when you are deliberately at a session boundary.
SETTLED="$CAL/rejected_candidates_settled.jsonl"
SETTLED_THRESHOLD_BYTES=$((400 * 1024 * 1024))
ARCHIVER="$REPO/scripts/archive_ghost_logs.py"
VENV_PY="$REPO/.venv/bin/python"
SETTLED_CUTOFF_DAYS=3

if [ ! -f "$SETTLED" ]; then
  log "SETTLED SKIP: no file at $SETTLED"
elif [ ! -x "$VENV_PY" ] || [ ! -f "$ARCHIVER" ]; then
  log "SETTLED SKIP: archiver or venv python missing"
elif [ "$INGEST_OK" != "1" ]; then
  # Never trim the jsonl when the warehouse did not take this run's rows — that
  # is the one ordering that could actually lose ghost history.
  log "SETTLED SKIP: DuckDB ingest did not succeed; refusing to archive un-warehoused rows"
else
  S_SIZE=$(stat -f%z "$SETTLED")
  S_MB=$((S_SIZE / 1024 / 1024))
  if [ "$S_SIZE" -lt "$SETTLED_THRESHOLD_BYTES" ]; then
    log "SETTLED SKIP: ${S_MB}MB < threshold=$((SETTLED_THRESHOLD_BYTES / 1024 / 1024))MB"
  elif [ "${PSB_FORCE_SETTLED_ARCHIVE:-0}" != "1" ] && pgrep -f "src/main.py" >/dev/null 2>&1; then
    log "SETTLED DEFER: bot is trading (${S_MB}MB) — index rebuild would stall the scan loop; retrying next run"
  else
    log "SETTLED ARCHIVE: start (${S_MB}MB, cutoff=${SETTLED_CUTOFF_DAYS}d)"
    if nice -n 19 "$VENV_PY" "$ARCHIVER" --cutoff-days "$SETTLED_CUTOFF_DAYS" --execute >>"$LOGFILE" 2>&1; then
      S_NEW_MB=$(( $(stat -f%z "$SETTLED") / 1024 / 1024 ))
      log "SETTLED ARCHIVE: done ${S_MB}MB -> ${S_NEW_MB}MB"
    else
      log "SETTLED ARCHIVE: FAILED (non-zero exit) — live files left intact, see $LOGFILE"
    fi
  fi
fi
# ─────────────────────────────────────────────────────────────────────────────

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
