"""Backfill rejected_candidates.jsonl from polybot strategy log lines.

Recovers ghost-trade records for hist-gate rejections that happened BEFORE the
in-process logger was added. Joins the rejection log lines against today's paper
trade journals (entries.jsonl) which carry the full market_id ↔ question mapping.

Usage:
    python tools/backfill_rejected_candidates.py [--log data/logs/polybot_20260515.log]
                                                  [--paper-trades-dir data/paper_trades]
                                                  [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / "data" / "logs" / "polybot_20260515.log"
DEFAULT_OUT = REPO_ROOT / "data" / "calibration" / "rejected_candidates.jsonl"
DEFAULT_PAPER_DIR = REPO_ROOT / "data" / "paper_trades"

# Match strategy log line:
# 2026-05-15 06:01:28,617 - src.strategies.bitcoin - INFO -   BTC [5m] skip 'Bitcoin Up or Down - May 15, 9:05AM-9:10' — 4H falling, 1H also falling — no momentum building for LONG
REJECT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d* - src\.strategies\.bitcoin - \w+ -\s+BTC \[(?P<window>5m|15m)\] skip '(?P<question>[^']+)' .*no momentum building for (?P<side>LONG|SHORT)"
)

# Log timestamps are in PDT (UTC-7) for May (PDT/MDT). Adjust if your bot writes UTC directly.
# We infer offset by comparing log time to OPS_JSON ts, but for simplicity assume PDT.
LOG_TZ_OFFSET_HOURS = -7


def build_market_index(paper_dir: Path) -> Dict[str, str]:
    """Walk all session entries.jsonl files, build {question_prefix_40: market_id}."""
    idx: Dict[str, str] = {}
    if not paper_dir.exists():
        return idx
    for sess in sorted(paper_dir.iterdir()):
        ej = sess / "entries.jsonl"
        if not ej.exists():
            continue
        try:
            with open(ej) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    mid = o.get("market_id", "")
                    q = o.get("market_question", "")
                    if not (mid and q and "bitcoin" in q.lower() and ("up or down" in q.lower() or "updown" in q.lower())):
                        continue
                    # Match the log truncation: market.question[:40]
                    key = q[:40]
                    if key and key not in idx:
                        idx[key] = mid
                    # Also key the full question in case truncation differs
                    if q and q not in idx:
                        idx[q] = mid
        except OSError:
            continue
    return idx


def parse_question_for_window(q: str) -> Optional[Tuple[datetime, datetime]]:
    """From 'Bitcoin Up or Down - May 15, 9:05AM-9:10AM ET' parse start/end UTC.

    Truncated forms ('9:05AM-9:10') still carry start time and we infer end from `window`.
    Returns (start_utc, end_utc) or None.
    """
    # Match 'Month Day, H:MM(AM|PM)-...'
    m = re.search(r"(\w+) (\d{1,2}), (\d{1,2}:\d{2})(AM|PM)\s*-\s*(\d{1,2}:\d{2})?(AM|PM)?", q)
    if not m:
        return None
    month, day, t1, ap1, t2, ap2 = m.groups()
    try:
        year = datetime.now(timezone.utc).year
        # ET = EDT in May = UTC-4
        et_offset = timezone(timedelta(hours=-4))
        start_naive = datetime.strptime(f"{year} {month} {day} {t1}{ap1}", "%Y %B %d %I:%M%p")
        start_et = start_naive.replace(tzinfo=et_offset)
        if t2 and ap2:
            end_naive = datetime.strptime(f"{year} {month} {day} {t2}{ap2}", "%Y %B %d %I:%M%p")
            end_et = end_naive.replace(tzinfo=et_offset)
            # Handle midnight rollover
            if end_et < start_et:
                end_et += timedelta(days=1)
        else:
            end_et = start_et  # caller will add window
        return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)
    except (ValueError, KeyError):
        return None


def parse_log_lines(log_path: Path) -> List[Dict]:
    """Extract one record per BTC hist-gate rejection in the polybot log."""
    out = []
    with open(log_path, errors="ignore") as f:
        for raw in f:
            m = REJECT_RE.search(raw)
            if not m:
                continue
            d = m.groupdict()
            # Convert log time (assumed PDT) to UTC
            try:
                naive = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")
                utc_ts = (naive - timedelta(hours=LOG_TZ_OFFSET_HOURS)).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            window = d["window"]
            side = d["side"]
            action = "BUY_YES" if side == "LONG" else "BUY_NO"
            reason = f"hist_gate_{window}_{side.lower()}_reject"
            out.append({
                "ts": utc_ts.isoformat(),
                "window": window,
                "side": side,
                "action": action,
                "reason": reason,
                "question_truncated": d["question"],
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--paper-trades-dir", default=str(DEFAULT_PAPER_DIR))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"log not found: {log_path}", file=sys.stderr); return 1

    print(f"Building market index from {args.paper_trades_dir}...")
    idx = build_market_index(Path(args.paper_trades_dir))
    print(f"  indexed {len(idx)} question→market_id mappings")

    print(f"Parsing rejections from {log_path}...")
    rejections = parse_log_lines(log_path)
    print(f"  parsed {len(rejections)} rejection log lines")

    matched = 0
    unmatched = 0
    out_records = []
    seen_keys = set()  # (ts, question_truncated, side) — dedup if log was double-written
    for r in rejections:
        key = (r["ts"], r["question_truncated"], r["side"])
        if key in seen_keys:
            continue
        seen_keys.add(key)

        q_trunc = r["question_truncated"]
        # Try the 40-char key first, then look for a longer key starting with it
        mid = idx.get(q_trunc)
        full_q = q_trunc
        if not mid:
            for k, v in idx.items():
                if k.startswith(q_trunc):
                    mid = v
                    full_q = k
                    break

        if not mid:
            unmatched += 1
            continue

        # Compute end_ts: parse start time from question + add window minutes
        win_min = 5 if r["window"] == "5m" else 15
        parsed = parse_question_for_window(full_q)
        end_iso = ""
        if parsed:
            start_utc, end_utc = parsed
            # If end was truncated (only start parsed), compute it
            if end_utc == start_utc:
                end_utc = start_utc + timedelta(minutes=win_min)
            end_iso = end_utc.isoformat()

        rec = {
            "ts": r["ts"],
            "schema_version": 1,
            "strategy": "bitcoin",
            "window": r["window"],
            "side": r["side"],
            "action": r["action"],
            "reason": r["reason"],
            "lane_id": f"bitcoin|{r['window']}|{'up' if r['action']=='BUY_YES' else 'down'}|unknown|rejected",
            "market_id": str(mid),
            "market_question": full_q,
            "market_slug": "",
            "market_end_ts": end_iso,
            "token_id_yes": "",
            "token_id_no": "",
            "yes_price": None,   # not recoverable from log
            "no_price": None,
            "est_prob_up": None,
            "htf_bias": None,
            "context": {"backfilled_from_log": True},
        }
        out_records.append(rec)
        matched += 1

    print(f"  matched: {matched}   unmatched: {unmatched}")

    if not args.dry_run and out_records:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Read existing keys to avoid duplicates if backfill is re-run
        existing_keys = set()
        if out_path.exists():
            with open(out_path) as f:
                for line in f:
                    try: o = json.loads(line)
                    except: continue
                    existing_keys.add((o.get("ts"), o.get("market_id"), o.get("reason")))
        new_count = 0
        with open(out_path, "a") as f:
            for r in out_records:
                k = (r["ts"], r["market_id"], r["reason"])
                if k in existing_keys:
                    continue
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
                new_count += 1
        print(f"  appended {new_count} new records to {out_path}")
        print(f"  (skipped {len(out_records) - new_count} that were already present)")
    elif args.dry_run:
        print(f"  DRY RUN — would have written {len(out_records)} records to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
