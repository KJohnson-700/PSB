#!/usr/bin/env python3
"""PSB data lifecycle — tiered retention to keep disk + load-memory bounded.

One deterministic pass that implements the agreed policy:

  HOT (live, uncompressed)   COLD (gzip archive)        PURGE (deleted)
  ────────────────────────   ────────────────────       ───────────────
  ghost cal     <= 14d       14d .. 90d                 > 90d
  daily logs    <=  7d        7d .. 90d                 > 90d
  paper_trades  <= 14d       14d .. 90d                 > 90d
  cron output   <= 30d       (n/a)                      > 30d

Design rules:
  * Ghost rotation delegates to scripts/archive_ghost_logs.py, whose safety
    contract never archives an UNSETTLED candidate — the settle loop can't lose
    a row. We just call it with cutoff=14d.
  * Only OLD, DATED, non-live files are touched directly here. The live files
    (polybot.log via launchd, ops_pulse.jsonl via the writer) are NOT moved —
    ops_pulse self-rotates in src/ops_pulse.py; polybot.log is launchd-owned.
  * Cold archives are gzipped (~10x) and kept until the PURGE age, so audits and
    calibration history survive well past the hot window.
  * DRY-RUN by default. Pass --execute to actually modify the filesystem.

Usage:
    python scripts/psb_data_lifecycle.py                 # dry-run report
    python scripts/psb_data_lifecycle.py --execute       # apply
    python scripts/psb_data_lifecycle.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
LOGS = DATA / "logs"
CAL = DATA / "calibration"
PAPER = DATA / "paper_trades"
CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"

# Retention policy (days)
GHOST_HOT_DAYS = 14
LOG_HOT_DAYS = 7
PAPER_HOT_DAYS = 14
CRON_OUTPUT_DAYS = 30
COLD_PURGE_DAYS = 90  # delete gzipped archives older than this

NOW = datetime.now(timezone.utc)


def _mb(n: int) -> float:
    return round(n / 1024 / 1024, 1)


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _mtime_age_days(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 86400.0
    except OSError:
        return 0.0


def _gzip_to(src: Path, archive_dir: Path, execute: bool) -> int:
    """Gzip src into archive_dir, remove src. Returns bytes reclaimed (orig size)."""
    size = src.stat().st_size
    if not execute:
        return size
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / (src.name + ".gz")
    with src.open("rb") as fh, gzip.open(dest, "wb") as gz:
        shutil.copyfileobj(fh, gz)
    src.unlink()
    return size


def rotate_dated_logs(execute: bool) -> dict:
    """Gzip dated polybot_*.log older than LOG_HOT_DAYS to logs/archive/."""
    archive = LOGS / "archive"
    moved, freed = 0, 0
    for f in sorted(LOGS.glob("polybot_2026*.log")):
        if _mtime_age_days(f) > LOG_HOT_DAYS:
            freed += _gzip_to(f, archive, execute)
            moved += 1
    return {"step": "dated_logs_archived", "files": moved, "reclaimed_mb": _mb(freed)}


def rotate_ghosts(execute: bool) -> dict:
    """Delegate to the safe ghost archiver with the 14d hot cutoff."""
    archiver = REPO / "scripts" / "archive_ghost_logs.py"
    if not archiver.exists():
        return {"step": "ghosts", "error": f"archiver not found: {archiver}"}
    py = str(REPO / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    cmd = [py, str(archiver), "--cutoff-days", str(GHOST_HOT_DAYS)]
    if execute:
        cmd.append("--execute")
    try:
        r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=1800)
        tail = (r.stdout or "").strip().splitlines()[-6:]
        return {"step": "ghosts_archived", "cutoff_days": GHOST_HOT_DAYS,
                "exit": r.returncode, "tail": tail,
                "stderr": (r.stderr or "").strip()[:200] or None}
    except Exception as e:
        return {"step": "ghosts", "error": f"{type(e).__name__}: {e}"}


def archive_old_paper_sessions(execute: bool) -> dict:
    """tar.gz paper_trades session dirs older than PAPER_HOT_DAYS."""
    if not PAPER.exists():
        return {"step": "paper_trades", "files": 0, "reclaimed_mb": 0.0}
    archive = PAPER / "archive"
    moved, freed = 0, 0
    for d in sorted(PAPER.iterdir()):
        if not d.is_dir() or d.name == "archive":
            continue
        if _mtime_age_days(d) <= PAPER_HOT_DAYS:
            continue
        size = _dir_size(d)
        if execute:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.make_archive(str(archive / d.name), "gztar", root_dir=str(d))
            shutil.rmtree(d)
        freed += size
        moved += 1
    return {"step": "paper_sessions_archived", "dirs": moved, "reclaimed_mb": _mb(freed)}


def prune_cron_output(execute: bool) -> dict:
    if not CRON_OUTPUT.exists():
        return {"step": "cron_output", "files": 0, "reclaimed_mb": 0.0}
    removed, freed = 0, 0
    for job_dir in CRON_OUTPUT.iterdir():
        if not job_dir.is_dir():
            continue
        for f in job_dir.iterdir():
            if _mtime_age_days(f) > CRON_OUTPUT_DAYS:
                try:
                    sz = f.stat().st_size
                    if execute:
                        f.unlink()
                    removed += 1
                    freed += sz
                except OSError:
                    pass
    return {"step": "cron_output_pruned", "files": removed, "reclaimed_mb": _mb(freed)}


def purge_cold_archives(execute: bool) -> dict:
    """Delete gzipped archives older than COLD_PURGE_DAYS across data/."""
    removed, freed = 0, 0
    for archive_dir in DATA.rglob("archive"):
        if not archive_dir.is_dir():
            continue
        for f in archive_dir.iterdir():
            if f.suffix in (".gz", ".tgz") or f.name.endswith(".tar.gz"):
                if _mtime_age_days(f) > COLD_PURGE_DAYS:
                    try:
                        sz = f.stat().st_size
                        if execute:
                            f.unlink()
                        removed += 1
                        freed += sz
                    except OSError:
                        pass
    return {"step": "cold_archives_purged", "files": removed, "reclaimed_mb": _mb(freed)}


def main() -> int:
    ap = argparse.ArgumentParser(description="PSB tiered data lifecycle")
    ap.add_argument("--execute", action="store_true", help="apply changes (default dry-run)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    before = {p.name: _dir_size(p) for p in (LOGS, CAL, PAPER, DATA)}
    steps = [
        rotate_dated_logs(args.execute),
        rotate_ghosts(args.execute),
        archive_old_paper_sessions(args.execute),
        prune_cron_output(args.execute),
        purge_cold_archives(args.execute),
    ]
    after = {p.name: _dir_size(p) for p in (LOGS, CAL, PAPER, DATA)} if args.execute else before

    report = {
        "ts": NOW.isoformat(),
        "mode": "EXECUTE" if args.execute else "DRY-RUN",
        "policy": {"ghost_hot_days": GHOST_HOT_DAYS, "log_hot_days": LOG_HOT_DAYS,
                   "paper_hot_days": PAPER_HOT_DAYS, "cold_purge_days": COLD_PURGE_DAYS},
        "data_total_mb_before": _mb(before["data"]),
        "data_total_mb_after": _mb(after["data"]) if args.execute else None,
        "steps": steps,
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"PSB data lifecycle — {report['mode']} — {NOW.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 64)
    print(f"data/ total before: {report['data_total_mb_before']} MB")
    for s in steps:
        if "error" in s:
            print(f"  [{s['step']}] ERROR: {s['error']}")
        elif "reclaimed_mb" in s:
            n = s.get("files", s.get("dirs", 0))
            print(f"  [{s['step']}] {n} items, {s['reclaimed_mb']} MB")
        else:
            print(f"  [{s['step']}] exit={s.get('exit')}")
            for t in s.get("tail", []):
                print(f"      {t}")
    if args.execute:
        print(f"data/ total after:  {report['data_total_mb_after']} MB")
    else:
        print("(dry-run — re-run with --execute to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
