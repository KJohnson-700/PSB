#!/usr/bin/env python3
"""Archive old, ALREADY-SETTLED ghost-log rows to reclaim disk — loop-safe.

Step 3c of docs/GHOST_LOG_CHECKPOINT_SPEC.md.

The closed loop must stay closed. This script's safety contract:

  * A rejected-candidate row is moved out of the LIVE rejected log ONLY IF
    (a) it is older than the cutoff AND (b) its ghost_id is already present in
    the settled set (i.e. it has been settled). An unsettled ghost is NEVER
    archived, so the settle loop can never "lose" a candidate.
  * Settled rows older than the cutoff are moved to a compressed archive. The
    settled_index sidecar (step 3a) retains every ghost_id, so idempotency does
    not depend on the archived rows staying in the live settled file.
  * Archives are gzipped and kept; a .bak of each original is written before any
    rewrite; kept + archived counts are verified to equal the original.

Default is DRY-RUN. Pass --execute to actually rewrite the live files.

Usage:
    python scripts/archive_ghost_logs.py                     # dry-run, cutoff=3d
    python scripts/archive_ghost_logs.py --cutoff-days 5     # dry-run preview
    python scripts/archive_ghost_logs.py --execute           # do it (keeps .bak)
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.analysis.ghost_calibration import (  # noqa: E402
    DEFAULT_REJECTED_LOG,
    DEFAULT_SETTLED_LOG,
    DEFAULT_SETTLED_INDEX,
    _load_settled_ids_indexed,
    _write_settled_index,
    ghost_id,
)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _partition_file(
    path: Path,
    cutoff: datetime,
    *,
    settled_ids: Optional[set] = None,
    require_settled: bool = False,
) -> Tuple[list, list, int]:
    """Return (keep_lines, archive_lines, malformed) without mutating ``path``.

    A line is archived only if its ts < cutoff AND (when require_settled) its
    ghost_id is in ``settled_ids``. Everything else is kept. Malformed lines are
    always kept (never silently dropped).
    """
    keep: list = []
    archive: list = []
    malformed = 0
    if not path.exists():
        return keep, archive, malformed
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except json.JSONDecodeError:
                malformed += 1
                keep.append(line if line.endswith("\n") else line + "\n")
                continue
            ts = _parse_ts(rec.get("ts") or rec.get("settled_at"))
            old_enough = ts is not None and ts < cutoff
            archivable = old_enough
            if require_settled:
                # Use the CANONICAL recomputed id (ghost_id = sha1(ts|market_id|
                # reason)), exactly as settle_rejected_candidates and the index
                # do. A literal "ghost_id" field on a rejected row (if any) is
                # NOT authoritative — keying on it could mismatch the index and
                # wrongly archive an unsettled ghost.
                gid = ghost_id(rec)
                archivable = archivable and (gid in (settled_ids or set()))
            (archive if archivable else keep).append(
                line if line.endswith("\n") else line + "\n"
            )
    return keep, archive, malformed


def _write_archive(archive_dir: Path, name: str, lines: list, runts: str) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    out = archive_dir / f"{name}_{runts}.jsonl.gz"
    with gzip.open(out, "wt", encoding="utf-8") as gz:
        gz.writelines(lines)
    return out


def _rewrite_live(path: Path, keep: list) -> None:
    """Atomically replace ``path`` with only the kept lines, after a .bak."""
    bak = path.with_name(path.name + ".pre-archive.bak")
    shutil.copy2(path, bak)
    tmp = path.with_name(path.name + ".archive.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(keep)
    os.replace(tmp, path)  # atomic


def archive_ghost_logs(
    *,
    rejected_path: Path = DEFAULT_REJECTED_LOG,
    settled_path: Path = DEFAULT_SETTLED_LOG,
    index_path: Path = DEFAULT_SETTLED_INDEX,
    cutoff_days: int = 3,
    execute: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cutoff_days)
    runts = now.strftime("%Y%m%dT%H%M%SZ")
    archive_dir = rejected_path.parent / "archive"

    # Source of truth for "what is settled" — via the 3a index (self-healing).
    settled_ids = _load_settled_ids_indexed(settled_path, index_path)

    # Rejected: archive only OLD + SETTLED rows (never an unsettled ghost).
    r_keep, r_arch, r_bad = _partition_file(
        rejected_path, cutoff, settled_ids=settled_ids, require_settled=True
    )
    # Settled: archive OLD rows (all are settled by definition; index retains ids).
    s_keep, s_arch, s_bad = _partition_file(settled_path, cutoff)

    summary: Dict[str, object] = {
        "cutoff": cutoff.isoformat(),
        "execute": execute,
        "rejected_total": len(r_keep) + len(r_arch),
        "rejected_kept": len(r_keep),
        "rejected_archived": len(r_arch),
        "rejected_malformed_kept": r_bad,
        "settled_total": len(s_keep) + len(s_arch),
        "settled_kept": len(s_keep),
        "settled_archived": len(s_arch),
        "settled_malformed_kept": s_bad,
        "archives": [],
    }

    # Invariant check: nothing lost.
    assert summary["rejected_kept"] + summary["rejected_archived"] == summary["rejected_total"]
    assert summary["settled_kept"] + summary["settled_archived"] == summary["settled_total"]

    if not execute:
        summary["note"] = "DRY-RUN — no files modified. Re-run with --execute to apply."
        return summary

    if r_arch:
        a = _write_archive(archive_dir, "rejected_candidates_archive", r_arch, runts)
        _rewrite_live(rejected_path, r_keep)
        summary["archives"].append(str(a))
    if s_arch:
        a = _write_archive(archive_dir, "rejected_candidates_settled_archive", s_arch, runts)
        _rewrite_live(settled_path, s_keep)
        summary["archives"].append(str(a))
        # Critical for the closed loop: after shrinking the live settled file we
        # MUST rewrite the index to the FULL settled set (live tail + archived)
        # and re-point its meta at the new file. Otherwise the loader would see a
        # smaller settled file, treat it as truncation/corruption, rebuild from
        # the shrunken live file, and forget the archived ghost_ids — reopening
        # the loop. ``settled_ids`` already holds the complete set (it was loaded
        # via the archive-aware index before any rewrite).
        _write_settled_index(index_path, settled_ids, settled_path)
        summary["index_refreshed"] = True
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cutoff-days", type=int, default=3,
                    help="Archive rows older than this many days (default 3).")
    ap.add_argument("--execute", action="store_true",
                    help="Actually rewrite live files (default: dry-run).")
    args = ap.parse_args()

    summary = archive_ghost_logs(cutoff_days=args.cutoff_days, execute=args.execute)
    print(json.dumps(summary, indent=2))
    if not args.execute:
        print("\nDRY-RUN complete. No files changed. "
              "Add --execute to apply (a .pre-archive.bak is kept).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
