#!/usr/bin/env python3
"""Out-of-process ghost settler (2026-07-30).

Restores the rejected-candidate -> settled feedback loop that was SEVERED from the
trading hot path on 2026-07-13 (main.py auto_settle_enabled:false runs only the live
settler and bails before ghost work — deliberately, to keep the fat 788MB reject-log
scan off the scan/exit cycle). This runs the SAME `settle_rejected_candidates()` logic
(idempotent, checkpoint-indexed, archive-aware) in a separate process so
`data/calibration/rejected_candidates_settled.jsonl` starts populating again — i.e. we
learn which blocked lanes WOULD have won, without touching the bot's latency.

Usage:
  python scripts/settle_rejected_candidates.py --once            # one full pass, exit
  python scripts/settle_rejected_candidates.py --loop --interval 900   # every 15 min
  python scripts/settle_rejected_candidates.py --once --throttle 0.05  # gentle API pacing

Safe to run alongside the live bot: it only READS the reject log and APPENDS to the
settled log (+ its index). Idempotent — re-running never double-settles (ghost_id skip).
"""
import argparse
import glob
import gzip
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.ghost_calibration import settle_rejected_candidates  # noqa: E402

CALIB = ROOT / "data" / "calibration"
OFFSET_FILE = CALIB / "settle_read_offset.json"
PENDING_FILE = CALIB / "settle_pending.jsonl"
REJECT_LOG = CALIB / "rejected_candidates.jsonl"
ARCHIVE_DIR = CALIB / "archive"
PRUNE_DAYS = 90

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - ghost_settle - %(levelname)s - %(message)s",
)
log = logging.getLogger("ghost_settle")


def _load_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text()).get("offset", 0))
    except Exception:
        return 0  # first run / corrupt -> full backlog scan, then checkpointed


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(json.dumps({"offset": int(offset)}))
    except OSError as exc:
        log.warning("offset checkpoint write failed: %s", exc)


def _maybe_rotate(threshold_mb: float) -> bool:
    """Bound the raw reject-log growth WITHOUT launchd (which is TCC-blocked from
    ~/Documents on Sequoia without Full Disk Access). Runs in-process with the settler,
    which inherits the terminal's file access. Safe to call AFTER a settle pass: the
    pending queue already holds every unsettled-but-retryable row, so archiving the raw
    log loses no settlement evidence. Mirrors scripts/rotate_ghost_logs.sh: atomic mv
    (logger recreates via O_CREAT on next append), gzip, prune > PRUNE_DAYS. Resets the
    read offset so the next pass starts clean on the fresh log."""
    if threshold_mb <= 0 or not REJECT_LOG.exists():
        return False
    size = REJECT_LOG.stat().st_size
    if size < threshold_mb * 1024 * 1024:
        return False
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = ARCHIVE_DIR / f"rejected_candidates_archive_{stamp}.jsonl"
    try:
        os.replace(REJECT_LOG, dest)  # atomic within the same filesystem
    except OSError as exc:
        log.warning("rotate mv failed: %s", exc)
        return False
    time.sleep(2)  # let any append whose open() beat the mv finish landing in dest
    try:
        with open(dest, "rb") as src, gzip.open(f"{dest}.gz", "wb") as gz:
            shutil.copyfileobj(src, gz, length=1024 * 1024)
        os.remove(dest)
    except OSError as exc:
        log.warning("rotate gzip failed (archive left uncompressed): %s", exc)
    _save_offset(0)  # fresh live log -> read from the start next pass
    # prune compressed archives older than PRUNE_DAYS
    cutoff = time.time() - PRUNE_DAYS * 86400
    pruned = 0
    for gzf in glob.glob(str(ARCHIVE_DIR / "rejected_candidates_archive_*.jsonl.gz")):
        try:
            if os.path.getmtime(gzf) < cutoff:
                os.remove(gzf)
                pruned += 1
        except OSError:
            pass
    log.info("ROTATED reject log %.0fMB -> %s.gz | pruned %d old archive(s)",
             size / 1024 / 1024, dest.name, pruned)
    return True


def _one_pass(throttle: float, dry_run: bool, incremental: bool) -> dict:
    t0 = time.monotonic()
    kwargs = {"throttle_sec": throttle, "dry_run": dry_run}
    if incremental:
        kwargs["start_offset"] = _load_offset()
        kwargs["pending_path"] = PENDING_FILE
    summary = settle_rejected_candidates(**kwargs)
    dt = time.monotonic() - t0
    # Advance the checkpoint ONLY when the settle+pending writes succeeded (checkpoint_ok);
    # otherwise keep the old offset so the next pass re-reads those bytes (idempotent).
    if (
        incremental
        and not dry_run
        and summary.get("checkpoint_ok", False)
        and summary.get("end_offset") is not None
    ):
        _save_offset(summary["end_offset"])
    log.info(
        "pass %.1fs | newly=%s written=%s already=%s too_recent=%s unresolved/api=%s "
        "no_mkt=%s regime_matched=%s pending=%s end_offset=%s",
        dt,
        summary.get("newly_settled"),
        summary.get("written"),
        summary.get("already_settled"),
        summary.get("too_recent"),
        summary.get("unresolved_or_api"),
        summary.get("no_market_id"),
        summary.get("regime_matched"),
        summary.get("pending"),
        summary.get("end_offset"),
    )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    ap.add_argument("--loop", action="store_true", help="run passes forever with --interval sleep")
    ap.add_argument("--interval", type=float, default=900.0, help="seconds between loop passes")
    ap.add_argument("--throttle", type=float, default=0.0, help="sleep between resolution fetches (API pacing)")
    ap.add_argument("--dry-run", action="store_true", help="compute but do not write settled rows")
    ap.add_argument("--full-scan", action="store_true", help="disable incremental offset+pending (re-read whole log)")
    ap.add_argument("--rotate-mb", type=float, default=200.0,
                    help="rotate+archive the raw reject log when it exceeds this many MB (0=off). "
                         "Runs AFTER each settle pass (pending queue preserves unsettled rows).")
    args = ap.parse_args()

    incremental = not args.full_scan
    if not args.once and not args.loop:
        args.once = True  # default to a single pass

    if args.once:
        s = _one_pass(args.throttle, args.dry_run, incremental)
        if not args.dry_run:
            _maybe_rotate(args.rotate_mb)
        print(json.dumps(s))
        return 0

    log.info("ghost settle loop starting (interval=%.0fs throttle=%.2fs incremental=%s rotate_mb=%.0f)",
             args.interval, args.throttle, incremental, args.rotate_mb)
    while True:
        try:
            _one_pass(args.throttle, args.dry_run, incremental)
            if not args.dry_run:
                _maybe_rotate(args.rotate_mb)
        except KeyboardInterrupt:
            log.info("interrupted — exiting loop")
            return 0
        except Exception as exc:  # keep the daemon alive across transient errors
            log.warning("settle pass failed (continuing): %s", exc)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
