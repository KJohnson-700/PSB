"""Backfill lane_family / lane_id on trades.jsonl by joining trade_id against
paper_trades/<session>/entries.jsonl, which preserves the original signal
reason text. Recovers drift/spike/predict_window classifications that the
pre-fix resolve_entry_family precedence silently dropped.
"""
from __future__ import annotations

import glob
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis.lane_identity import resolve_entry_family  # noqa: E402

TRADES = ROOT / "data/calibration/trades.jsonl"
PAPER_SESSIONS_GLOB = str(ROOT / "data/paper_trades/test_*")


def build_reason_map() -> dict[str, str]:
    """trade_id -> concatenated reason text from paper_trades entries."""
    out: dict[str, str] = {}
    for sess_dir in sorted(glob.glob(PAPER_SESSIONS_GLOB)):
        entries = Path(sess_dir) / "entries.jsonl"
        if not entries.exists():
            continue
        with entries.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = rec.get("trade_id") or ""
                if not tid:
                    continue
                extra = rec.get("extra") or {}
                parts = [
                    str(rec.get("reason", "")),
                    str(extra.get("reason", "")),
                    str(extra.get("signal_reason", "")),
                ]
                combined = " ".join(p for p in parts if p)
                if combined.strip():
                    prior = out.get(tid, "")
                    out[tid] = (prior + " " + combined).strip() if prior else combined
    return out


def derive_lane_side(row: dict, lane_id_parts: list[str]) -> str:
    side_raw = str(row.get("side") or row.get("action") or "").lower()
    if "yes" in side_raw:
        return "up"
    if "no" in side_raw:
        return "down"
    return lane_id_parts[2] if len(lane_id_parts) >= 3 else ""


def main() -> None:
    print("[1/3] indexing paper_trades reason text by trade_id...")
    reason_map = build_reason_map()
    print(f"      indexed {len(reason_map)} trade_ids with reason text")

    if not TRADES.exists():
        print(f"[abort] {TRADES} missing")
        return

    backup = TRADES.with_suffix(TRADES.suffix + ".bak-pre-lane-rebucket")
    if not backup.exists():
        shutil.copy2(TRADES, backup)
        print(f"[2/3] backup: {TRADES.name} -> {backup.name}")
    else:
        print(f"[2/3] backup {backup.name} already exists, leaving as-is")

    print("[3/3] rebucketing...")
    total = 0
    changed = 0
    matched_with_reason = 0
    transitions: Counter = Counter()
    out_lines: list[str] = []

    with backup.open() as f:
        for line in f:
            stripped = line.rstrip("\n")
            if not stripped.strip():
                out_lines.append(stripped)
                continue
            total += 1
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                out_lines.append(stripped)
                continue

            tid = row.get("trade_id") or ""
            signal_reason = reason_map.get(tid, "")
            if signal_reason:
                matched_with_reason += 1

            lane_id_parts = (row.get("lane_id") or "").split("|")
            while len(lane_id_parts) < 5:
                lane_id_parts.append("")
            old_family = lane_id_parts[4]
            regime = lane_id_parts[3]
            lane_side = derive_lane_side(row, lane_id_parts)

            new_family = resolve_entry_family(
                strategy=row.get("strategy"),
                window_size=row.get("window"),
                lane_side=lane_side,
                side_source=row.get("side_source"),
                resolver_path=row.get("resolver_path"),
                ai_used=bool(row.get("ai_used")),
                reason=signal_reason,
                signal_reason=signal_reason,
            )

            if new_family != old_family:
                changed += 1
                transitions[(old_family, new_family)] += 1
                lane_id_parts[4] = new_family
                row["lane_id"] = "|".join(lane_id_parts)
                row["lane_family"] = new_family

            out_lines.append(json.dumps(row, separators=(",", ":")))

    TRADES.write_text("\n".join(out_lines) + ("\n" if out_lines else ""))
    print()
    print(f"      total trade rows:           {total}")
    print(f"      matched to paper_trades:    {matched_with_reason}")
    print(f"      rebucketed (family changed):{changed}")
    print()
    print("      transitions (old -> new, count):")
    for (old, new), count in transitions.most_common(30):
        print(f"        {count:5d}  {old or '<empty>':35s} -> {new}")


if __name__ == "__main__":
    main()
