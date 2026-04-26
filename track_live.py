#!/usr/bin/env python3
"""
Live Strategy Win-Rate Tracker
================================
Run from the polymarket-bot folder:
    python track_live.py

Reads the current paper-trade journal and shows a live breakdown of:
  - BTC updown 15m  WR + PnL
  - BTC updown 5m   WR + PnL
  - SOL updown 15m  WR + PnL
  - SOL updown 5m   WR + PnL
  - Fade            WR + PnL

Also flags sessions: OLD CODE (before restart) vs NEW CODE (after restart).

Press Ctrl+C to exit.  Run with --once to print once and exit.
"""

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR   = Path(__file__).parent / "data" / "paper_trades"
LOG_DIR    = Path(__file__).parent / "src" / "logs"

# NEW CODE marker: first log line that only appears after our session edits
NEW_CODE_MARKERS = [
    "Anti-LTF gate passed",
    "4H histogram",
    "1H histogram",
]

# ── Helpers ────────────────────────────────────────────────────────────────────
def classify(q: str, strat: str) -> str:
    """Return category string for a market question."""
    if "Up or Down" in q:
        times = re.findall(r'(\d+):(\d+)(AM|PM)', q)
        window = 15
        if len(times) >= 2:
            h1, m1, p1 = times[0]
            h2, m2, p2 = times[1]
            # convert to absolute minutes
            def to_abs(h, m, p):
                h = int(h) % 12 + (12 if p == "PM" else 0)
                return h * 60 + int(m)
            diff = abs(to_abs(h2, m2, p2) - to_abs(h1, m1, p1))
            if diff == 0:
                diff = 5  # wrap-around (e.g. :55 to :00)
            window = diff
        if "Bitcoin" in q:
            sym = "BTC"
        elif "Ethereum" in q or "Eth " in q:
            sym = "ETH"
        else:
            sym = "SOL"
        size = "5m" if window <= 5 else "15m"
        return f"{sym}_updown_{size}"
    if strat in ("bitcoin", "sol_macro", "eth_macro"):
        return f"{strat}_threshold"
    return strat


def detect_new_code_start(log_path: Path) -> str | None:
    """
    Scan today's log for first timestamp where new-code markers appear.
    Returns ISO timestamp string or None.
    """
    if not log_path.exists():
        return None
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                for marker in NEW_CODE_MARKERS:
                    if marker in line:
                        # extract timestamp
                        m = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                        if m:
                            return m.group(1)
    except Exception:
        pass
    return None


def parse_journal(session_dir: Path, new_code_start: str | None):
    """Return two dicts (old_stats, new_stats) keyed by category."""
    entries_file = session_dir / "entries.jsonl"
    if not entries_file.exists():
        return {}, {}

    old_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "detail": []})
    new_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "detail": []})

    with open(entries_file, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue

            if e.get("event") != "EXIT":
                continue

            pnl   = float(e.get("pnl") or 0.0)
            strat = e.get("strategy", "unknown")
            q     = e.get("market_question", "")
            ts    = e.get("timestamp", "")[:19]  # YYYY-MM-DD HH:MM:SS
            cat   = classify(q, strat)

            # which bucket?
            is_new = (new_code_start is not None and ts >= new_code_start)
            bucket = new_stats if is_new else old_stats

            won = pnl > 0.01
            bucket[cat]["wins" if won else "losses"] += 1
            bucket[cat]["pnl"] += pnl
            bucket[cat]["detail"].append({
                "ts": ts, "action": e.get("action", ""), "pnl": pnl, "q": q[:55]
            })

    return old_stats, new_stats


def print_table(stats: dict, label: str):
    if not stats:
        print(f"  {label}: No trades yet.")
        return

    TARGET_CATS = [
        "BTC_updown_15m",
        "BTC_updown_5m",
        "SOL_updown_15m",
        "SOL_updown_5m",
    ]
    OTHER_CATS = [k for k in stats if k not in TARGET_CATS]

    print(f"\n  {'─'*58}")
    print(f"  {label}")
    print(f"  {'─'*58}")
    print(f"  {'STRATEGY':<22} {'N':>4} {'W':>4} {'L':>4} {'WR%':>6} {'PnL':>9}  {'STATUS'}")
    print(f"  {'─'*58}")

    total_n = total_w = total_l = 0
    total_pnl = 0.0

    for cat in TARGET_CATS + OTHER_CATS:
        if cat not in stats:
            if cat in TARGET_CATS:
                print(f"  {cat:<22} {'--':>4} {'--':>4} {'--':>4} {'  --':>6}  {'--':>9}  waiting...")
            continue
        d   = stats[cat]
        n   = d["wins"] + d["losses"]
        wr  = d["wins"] / n * 100 if n > 0 else 0
        pnl = d["pnl"]
        flag = ""
        if n >= 10:
            flag = "✅ OK" if wr >= 60 else ("⚠️  LOW" if wr >= 50 else "🔴 BAD")
        elif n > 0:
            flag = f"({n} trades — need more)"

        print(f"  {cat:<22} {n:>4} {d['wins']:>4} {d['losses']:>4} {wr:>5.1f}%  {pnl:>+8.2f}  {flag}")
        total_n += n; total_w += d["wins"]; total_l += d["losses"]; total_pnl += pnl

    print(f"  {'─'*58}")
    total_wr = total_w / total_n * 100 if total_n > 0 else 0
    print(f"  {'TOTAL':<22} {total_n:>4} {total_w:>4} {total_l:>4} {total_wr:>5.1f}%  {total_pnl:>+8.2f}")


def find_latest_session() -> Path | None:
    sessions = sorted(DATA_DIR.glob("*"), reverse=True)
    for s in sessions:
        if (s / "entries.jsonl").exists():
            return s
    return None


def get_today_log() -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return LOG_DIR / f"polybot_{today}.log"


def run(once=False):
    print("\n🔍  Polymarket Live Strategy Tracker")
    print("    Watching for new-code markers (anti-LTF gate, histogram gate)...")

    while True:
        session_dir = find_latest_session()
        if not session_dir:
            print("  No journal session found. Is the bot running?")
            if once:
                return
            time.sleep(15)
            continue

        log_path = get_today_log()
        new_code_start = detect_new_code_start(log_path)

        print(f"\n{'='*62}")
        print(f"  📅  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Session: {session_dir.name}")
        if new_code_start:
            print(f"  🟢  NEW CODE active since {new_code_start}")
        else:
            print(f"  🟡  OLD CODE — bot not yet restarted with new strategies")
        print(f"{'='*62}")

        old_stats, new_stats = parse_journal(session_dir, new_code_start)

        if new_code_start and new_stats:
            print_table(new_stats, "📊 NEW CODE RESULTS (post-restart)")
        if old_stats:
            print_table(old_stats, "📋 OLD CODE RESULTS (pre-restart)")

        if once:
            # Print trade detail for key strategies
            for stats_label, stats in [("NEW", new_stats), ("OLD", old_stats)]:
                for cat in ["BTC_updown_15m", "BTC_updown_5m", "SOL_updown_15m"]:
                    trades = stats.get(cat, {}).get("detail", [])
                    if trades:
                        print(f"\n  [{stats_label}] {cat} — last {min(10,len(trades))} trades:")
                        for t in trades[-10:]:
                            tag = "WIN " if t["pnl"] > 0 else "LOSS"
                            print(f"    [{tag}] {t['action']:9s} ${t['pnl']:+6.2f}  {t['q']}  {t['ts']}")
            return

        print(f"\n  Refreshing in 60s... (Ctrl+C to exit)")
        time.sleep(60)


if __name__ == "__main__":
    once = "--once" in sys.argv
    try:
        run(once=once)
    except KeyboardInterrupt:
        print("\n\nTracker stopped.")
