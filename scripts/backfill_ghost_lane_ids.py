#!/usr/bin/env python3
"""Backfill ``live_lane_id`` and pre-resolver-reject tagging on ghost log.

Two structural problems with the ghost log:

1. Records older than the rich-metadata writer (~2026-05-15) lack
   ``live_lane_id`` and ``lane_family``. They survive the translator's
   ``standard`` fallback and bucket together with real ``*|standard``
   lanes, polluting the standard bucket.
2. Some active code paths STILL emit records without resolver_path /
   side_source / lane_family — confirmed: every unstructured record has
   none of those three. These are rejections that fire BEFORE the lane
   direction resolver runs (pre-resolver gate rejects like
   ``iql_15m_reject``, ``eth_15m_weak_confirm``). They are not
   ``standard`` lane attempts; they are a separate population entirely.

This script:

- Adds ``live_lane_id`` to every record missing it, computed via the
  same translator the live aggregator uses (``_ghost_to_live_lane_id``).
- Tags pre-resolver rejections with ``lane_family: "pre_resolver_reject"``
  so they bucket separately from real standard-family attempts. The
  translator already prefers ``rec.get("lane_family")``, so the
  classification auto-corrects without any code change.
- Operates on settled + unsettled logs (unsettled defaults off; pass
  ``--include-unsettled`` to include both).

Default is dry-run — pass ``--apply`` to atomically rewrite.

WARNING: the bot may be actively appending to these files. With
``--apply``, the rewrite reads the entire file into memory, processes,
writes to ``.fixed``, then renames. Any rows the bot appended during
the rewrite would be lost. Pause the bot before applying, OR use
``--apply --append-only-safe`` which captures the file size first and
only rewrites the prefix, preserving any newly-appended tail.

Examples::

    # See what would change (no writes)
    python3 scripts/backfill_ghost_lane_ids.py --report

    # Apply to settled log only (safe variant — preserves new appends)
    python3 scripts/backfill_ghost_lane_ids.py --apply --append-only-safe

    # Apply to both files (after pausing the bot)
    python3 scripts/backfill_ghost_lane_ids.py --apply --include-unsettled
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.lane_thresholds import (
    DEFAULT_CALIBRATION_DIR,
    DEFAULT_SETTLED_LOG,
    _ghost_to_live_lane_id,
)

DEFAULT_UNSETTLED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"

PRE_RESOLVER_FAMILY = "pre_resolver_reject"


def _is_pre_resolver_reject(rec: Dict[str, Any]) -> bool:
    """A record is a pre-resolver reject when none of resolver_path,
    side_source, or lane_family are populated. Confirmed empirically:
    every unstructured record matches this. These rejections fired
    before the lane direction resolver assigned a family."""
    return (
        not rec.get("resolver_path")
        and not rec.get("side_source")
        and not rec.get("lane_family")
    )


def _process_record(rec: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Return (possibly-updated record, action_taken).

    Actions:
      - ``unchanged``  — record already has live_lane_id and a proper family
      - ``tagged_pre_resolver`` — added lane_family=pre_resolver_reject + live_lane_id
      - ``added_live_lane_id`` — record had lane_family but no live_lane_id
      - ``unfixable`` — translator could not produce a lane_id even after tagging
    """
    if rec.get("live_lane_id") and rec.get("lane_family"):
        return rec, "unchanged"

    updated = dict(rec)
    action = "added_live_lane_id"
    if _is_pre_resolver_reject(rec):
        updated["lane_family"] = PRE_RESOLVER_FAMILY
        action = "tagged_pre_resolver"

    new_lid = _ghost_to_live_lane_id(updated)
    if new_lid is None:
        return rec, "unfixable"
    updated["live_lane_id"] = new_lid
    return updated, action


def _iter_jsonl(path: Path) -> Iterable[Tuple[int, str, Optional[Dict[str, Any]]]]:
    """Yield (line_number, raw_line, parsed_record_or_None) for each line."""
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            raw = line.rstrip("\n")
            if not raw.strip():
                yield i, raw, None
                continue
            try:
                yield i, raw, json.loads(raw)
            except json.JSONDecodeError:
                yield i, raw, None


def backfill_file(
    path: Path,
    *,
    apply: bool,
    append_only_safe: bool,
) -> Dict[str, Any]:
    """Backfill one file. Returns a report dict."""
    if not path.exists():
        return {"path": str(path), "missing": True}

    capture_size = os.path.getsize(path) if append_only_safe else None

    actions: Counter[str] = Counter()
    out_lines: list[str] = []
    n_lines = 0
    n_parsed = 0
    sample_changed: list[Tuple[str, str]] = []  # (before_lane_id, after_live_lane_id)

    for i, raw, rec in _iter_jsonl(path):
        n_lines += 1
        if rec is None:
            out_lines.append(raw)
            actions["passthrough_nonjson"] += 1
            continue
        n_parsed += 1
        new_rec, action = _process_record(rec)
        actions[action] += 1
        if action in ("tagged_pre_resolver", "added_live_lane_id"):
            if len(sample_changed) < 3:
                sample_changed.append(
                    (rec.get("lane_id", ""), new_rec.get("live_lane_id", ""))
                )
        out_lines.append(json.dumps(new_rec))

    report: Dict[str, Any] = {
        "path": str(path),
        "lines": n_lines,
        "parsed": n_parsed,
        "actions": dict(actions),
        "sample_changes": sample_changed,
    }

    if not apply:
        return report

    n_change = actions["tagged_pre_resolver"] + actions["added_live_lane_id"]
    if n_change == 0:
        report["wrote"] = False
        report["reason"] = "no changes"
        return report

    tmp = path.with_suffix(path.suffix + ".fixed")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
        if out_lines:
            f.write("\n")

    if append_only_safe and capture_size is not None:
        new_size = os.path.getsize(path)
        if new_size > capture_size:
            # Append the bot's new writes to our tmp file before swap.
            with open(path, "rb") as src, open(tmp, "ab") as dst:
                src.seek(capture_size)
                tail = src.read()
                dst.write(tail)
            report["appended_tail_bytes"] = int(new_size - capture_size)

    backup = path.with_suffix(path.suffix + ".pre-backfill.bak")
    os.replace(path, backup)
    os.replace(tmp, path)
    report["wrote"] = True
    report["backup"] = str(backup)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill live_lane_id and pre-resolver-reject tags on ghost log"
    )
    ap.add_argument(
        "--settled",
        type=Path,
        default=DEFAULT_SETTLED_LOG,
        help="path to rejected_candidates_settled.jsonl",
    )
    ap.add_argument(
        "--unsettled",
        type=Path,
        default=DEFAULT_UNSETTLED_LOG,
        help="path to rejected_candidates.jsonl",
    )
    ap.add_argument(
        "--include-unsettled",
        action="store_true",
        help="also backfill rejected_candidates.jsonl (default: settled only)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="atomically rewrite files in place (default: dry-run, no writes)",
    )
    ap.add_argument(
        "--append-only-safe",
        action="store_true",
        help=(
            "with --apply, capture file size up-front and merge any tail "
            "the bot appended during the rewrite. Use when you cannot pause "
            "the bot."
        ),
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="print per-file report",
    )
    args = ap.parse_args()

    targets = [args.settled]
    if args.include_unsettled:
        targets.append(args.unsettled)

    mode = "APPLY" if args.apply else "DRY-RUN"
    if args.apply and args.append_only_safe:
        mode += " (append-only-safe)"
    print(f"=== {mode} ===")

    overall_action_totals: Counter[str] = Counter()
    for path in targets:
        report = backfill_file(
            path, apply=args.apply, append_only_safe=args.append_only_safe
        )
        print()
        print(f"--- {path} ---")
        if report.get("missing"):
            print("  file not present, skipping")
            continue
        print(f"  lines={report['lines']} parsed={report['parsed']}")
        print(f"  actions:")
        for action, count in sorted(report["actions"].items(), key=lambda x: -x[1]):
            print(f"    {action:30s}  {count:>8d}")
            overall_action_totals[action] += count
        if report.get("sample_changes"):
            print(f"  sample changes (first {len(report['sample_changes'])}):")
            for before, after in report["sample_changes"]:
                print(f"    {before!r} -> live_lane_id={after!r}")
        if args.apply:
            if report.get("wrote"):
                print(f"  WROTE: {path}")
                print(f"  backup: {report.get('backup')}")
                if report.get("appended_tail_bytes"):
                    print(
                        f"  merged tail of {report['appended_tail_bytes']} bytes "
                        f"appended by bot during rewrite"
                    )
            else:
                print(f"  not written ({report.get('reason')})")

    if len(targets) > 1:
        print()
        print("=== TOTAL ACROSS FILES ===")
        for action, count in sorted(overall_action_totals.items(), key=lambda x: -x[1]):
            print(f"  {action:30s}  {count:>8d}")

    if not args.apply:
        print()
        print("(dry-run — re-run with --apply to rewrite)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
