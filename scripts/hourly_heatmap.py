"""
Hourly Trade Heatmap — UTC hour performance analysis per strategy.

Shows a per-strategy table of WR, PnL, and trade count for every UTC hour.
Flags hours that are below the configured blocked threshold and highlights
which hours are already blocked in settings.yaml.

POLICY: This script is READ-ONLY. It never modifies settings.yaml or any
config file. All suggestions require explicit user approval before any AI
or human makes changes. Minimum ~7 days of live data before acting on any
hour pattern (small samples are noise, not signal).

Journal EXIT `extra` (newer bots) includes: `ts_utc`, `exit_ts_utc`, `hour_utc_entry`,
`hour_utc_exit`, `hour_pt`, `hour_pt_exit`, `hold_seconds`, `minutes_to_market_end`,
`dow_utc`, `dow_pt`, etc. Use `--hour-axis` to slice by entry vs exit and UTC vs Pacific.

Usage:
    python scripts/hourly_heatmap.py                   # all strategies, last 30 days
    python scripts/hourly_heatmap.py --strategy bitcoin --days 60
    python scripts/hourly_heatmap.py --hour-axis exit_pt  # Pacific, exit-time bucket
    python scripts/hourly_heatmap.py --min-trades 3    # lower threshold for small samples
    python scripts/hourly_heatmap.py --suggest         # show hours to consider blocking/unblocking
"""

import sys
import os
import argparse
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# ── Config ─────────────────────────────────────────────────────────────────
CONFIG_PATH  = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
DATA_DIR     = Path(__file__).resolve().parent.parent / "data" / "paper_trades"

BAD_WR_THRESHOLD  = 0.46   # flag hours below this WR
BAD_EV_THRESHOLD  = -2.0   # flag hours with avg PnL below this $/trade
MIN_TRADES        = 5      # ignore hours with fewer trades than this

STRATEGIES = ["bitcoin", "sol_macro", "eth_macro"]
STRATEGY_CONFIG_KEYS = {
    "bitcoin": "bitcoin",
    "sol_macro": "sol_macro",
    "eth_macro": "eth_macro",
}


# ── Data loading (reuses same logic as strategy_coach.py) ──────────────────

def load_trades(
    days_back: int = 30,
    strategy_filter: str = None,
    hour_axis: str = "entry_utc",
) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    trades = []

    if not DATA_DIR.exists():
        print(f"[heatmap] No trade data found at {DATA_DIR}")
        return trades

    for session_dir in sorted(DATA_DIR.iterdir()):
        if not session_dir.is_dir():
            continue
        journal_path = session_dir / "entries.jsonl"
        if not journal_path.exists():
            continue

        import json
        open_entries: dict = {}
        raw_trades: list = []

        with open(journal_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue

                etype = e.get("event") or e.get("event_type") or ""
                tid = e.get("trade_id", "")
                strategy = e.get("strategy", "")

                if strategy_filter and strategy != strategy_filter:
                    continue

                if etype == "ENTRY":
                    open_entries[tid] = e
                elif etype == "EXIT":
                    pnl = e.get("pnl", 0) or 0
                    ep = e.get("entry_price", 0) or 0
                    cp = e.get("current_price", 0) or 0
                    if ep > 0 and abs(ep + cp - 1.0) < 0.02:
                        continue
                    if abs(pnl) > 200:
                        continue

                    ts_str = e.get("timestamp", "")
                    try:
                        closed_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        closed_at = None
                    if closed_at and closed_at.tzinfo is None:
                        closed_at = closed_at.replace(tzinfo=timezone.utc)
                    if closed_at and closed_at < cutoff:
                        continue

                    extra = e.get("extra") or {}
                    if not extra and tid in open_entries:
                        extra = open_entries[tid].get("extra") or {}

                    # Prefer signal hour at entry; else bucket by exit timestamp (UTC).
                    hour_entry_utc = extra.get("hour_utc_entry", extra.get("hour_utc"))
                    if hour_entry_utc is None and closed_at is not None:
                        hour_entry_utc = closed_at.hour

                    if hour_axis == "entry_utc":
                        bucket_h = hour_entry_utc
                    elif hour_axis == "exit_utc":
                        bucket_h = extra.get("hour_utc_exit")
                        if bucket_h is None and closed_at is not None:
                            bucket_h = closed_at.hour
                    elif hour_axis == "entry_pt":
                        bucket_h = extra.get("hour_pt")
                        if bucket_h is None and closed_at is not None and ZoneInfo is not None:
                            bucket_h = closed_at.astimezone(
                                ZoneInfo("America/Los_Angeles")
                            ).hour
                    else:  # exit_pt
                        bucket_h = extra.get("hour_pt_exit")
                        if bucket_h is None and closed_at is not None and ZoneInfo is not None:
                            bucket_h = closed_at.astimezone(
                                ZoneInfo("America/Los_Angeles")
                            ).hour

                    raw_trades.append({
                        "strategy":  strategy,
                        "action":    e.get("action", ""),
                        "pnl":       pnl,
                        "hour_utc":  bucket_h,
                        "window":    extra.get("window_size"),
                        "htf_bias":  extra.get("htf_bias"),
                        "exit_reason": e.get("reason", ""),
                        "closed_at": ts_str,
                    })

        trades.extend(raw_trades)

    return trades


# ── Analysis ───────────────────────────────────────────────────────────────

def build_hourly_stats(trades: list) -> dict:
    """
    Returns:
        {strategy: {hour(0-23): {"trades": int, "wins": int, "pnl": float}}}
    """
    stats: dict = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0}))
    no_hour = 0

    for t in trades:
        h = t.get("hour_utc")
        if h is None:
            no_hour += 1
            continue
        s = t["strategy"]
        stats[s][int(h)]["trades"] += 1
        stats[s][int(h)]["pnl"]    += t["pnl"]
        if t["pnl"] > 0:
            stats[s][int(h)]["wins"] += 1

    return dict(stats), no_hour


def load_blocked_hours(config: dict) -> dict:
    """Returns {strategy: set of blocked hours}."""
    out = {}
    for strat, key in STRATEGY_CONFIG_KEYS.items():
        blocked = config.get("strategies", {}).get(key, {}).get("blocked_utc_hours_updown", [])
        out[strat] = set(blocked)
    return out


# ── Rendering ─────────────────────────────────────────────────────────────

def wr_bar(wr: float, width: int = 10) -> str:
    """Mini text bar for WR."""
    filled = round(wr * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def color(text: str, code: str) -> str:
    """ANSI color if terminal supports it."""
    if not sys.stdout.isatty():
        return text
    codes = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "bold": "1", "reset": "0"}
    return f"\033[{codes.get(code,'0')}m{text}\033[0m"


def print_heatmap(
    stats: dict,
    blocked: dict,
    min_trades: int,
    suggest: bool,
    hour_axis_label: str,
):
    print()
    print("=" * 78)
    print(f"  HOURLY PERFORMANCE HEATMAP  ({hour_axis_label})")
    print("=" * 78)

    all_strategies = sorted(stats.keys())
    if not all_strategies:
        print("  No trade data with hour_utc features found.")
        print("  Trades need to be logged with signal features (recent sessions only).")
        return

    for strategy in all_strategies:
        hour_data = stats[strategy]
        blocked_hrs = blocked.get(strategy, set())

        total = sum(v["trades"] for v in hour_data.values())
        wins  = sum(v["wins"]   for v in hour_data.values())
        pnl   = sum(v["pnl"]    for v in hour_data.values())
        overall_wr = wins / total if total else 0

        strat_label = strategy.upper().replace("_", " ")
        print(f"\n  {strat_label}  —  {total} trades  WR={overall_wr:.1%}  PnL=${pnl:+.2f}")
        print(f"  {'Hr':>3}  {'Trades':>6}  {'WR':>6}  {'Avg$/t':>7}  {'Bar':^12}  {'Status'}")
        print(f"  {'-'*3}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*12}  {'-'*20}")

        suggest_block   = []
        suggest_unblock = []

        for h in range(24):
            d = hour_data.get(h, {"trades": 0, "wins": 0, "pnl": 0.0})
            n  = d["trades"]
            wr = d["wins"] / n if n else None
            avg_pnl = d["pnl"] / n if n else 0.0
            is_blocked = h in blocked_hrs

            if n == 0:
                bar    = " " * 12
                status = color("BLOCKED", "yellow") if is_blocked else ""
                wr_str = "  --  "
                avg_str = "   --  "
            else:
                bar    = wr_bar(wr)
                wr_str = f"{wr:.1%}"
                avg_str = f"${avg_pnl:+.2f}"

                if n >= min_trades:
                    if wr < BAD_WR_THRESHOLD or avg_pnl < BAD_EV_THRESHOLD:
                        if is_blocked:
                            status = color("BLOCKED (justified)", "yellow")
                        else:
                            status = color("!! CONSIDER BLOCKING", "red")
                            suggest_block.append((h, n, wr, avg_pnl))
                    else:
                        if is_blocked:
                            status = color("BLOCKED (review — data looks ok)", "cyan")
                            suggest_unblock.append((h, n, wr, avg_pnl))
                        else:
                            status = color("OK", "green")
                else:
                    status = color(f"low sample ({n})", "yellow") if is_blocked else f"low sample ({n})"

            print(f"  {h:>3}  {n:>6}  {wr_str:>6}  {avg_str:>7}  {bar:^12}  {status}")

        # Suggestions
        if suggest and suggest_block:
            hrs = sorted(h for h, *_ in suggest_block)
            print(f"\n  --> SUGGEST ADDING to blocked_utc_hours_updown for {strategy}:")
            for h, n, wr, avg in suggest_block:
                print(f"      H{h:02d}  trades={n}  WR={wr:.1%}  avg=${avg:+.2f}/trade")
            existing = sorted(blocked_hrs)
            merged   = sorted(set(existing) | set(hrs))
            print(f"\n      Current:  {existing}")
            print(f"      Proposed: {merged}")

        if suggest and suggest_unblock:
            print(f"\n  --> SUGGEST REVIEWING blocks for {strategy} (data shows profitable):")
            for h, n, wr, avg in suggest_unblock:
                print(f"      H{h:02d}  trades={n}  WR={wr:.1%}  avg=${avg:+.2f}/trade")

    print()
    print("=" * 78)
    print("  KEY: !! = consider blocking  |  BLOCKED = already blocked")
    print("       low sample = fewer than min_trades — don't act yet")
    print("=" * 78)
    print()


def print_suggest_yaml(stats: dict, blocked: dict, min_trades: int):
    """Print ready-to-paste YAML for any suggested changes."""
    changes = {}
    for strategy in stats:
        hour_data = stats[strategy]
        blocked_hrs = blocked.get(strategy, set())
        new_blocks = set(blocked_hrs)

        for h in range(24):
            d = hour_data.get(h, {"trades": 0, "wins": 0, "pnl": 0.0})
            n = d["trades"]
            if n < min_trades:
                continue
            wr = d["wins"] / n
            avg_pnl = d["pnl"] / n
            if wr < BAD_WR_THRESHOLD or avg_pnl < BAD_EV_THRESHOLD:
                new_blocks.add(h)

        if new_blocks != blocked_hrs:
            changes[strategy] = sorted(new_blocks)

    if changes:
        print("\n  SUGGESTED settings.yaml CHANGES:")
        print("  (copy-paste into the relevant strategy section)\n")
        for strategy, hours in changes.items():
            key = STRATEGY_CONFIG_KEYS[strategy]
            print(f"  # {strategy}")
            print(f"  blocked_utc_hours_updown: {hours}\n")
    else:
        print("\n  No suggested changes — all blocked hours look justified.\n")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None

    parser = argparse.ArgumentParser(description="Hourly trade performance heatmap")
    parser.add_argument("--strategy", choices=STRATEGIES, default=None,
                        help="Filter to one strategy (default: all)")
    parser.add_argument("--days",     type=int, default=30,
                        help="Days of trade history to analyze (default: 30)")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES,
                        help=f"Min trades to report on an hour (default: {MIN_TRADES})")
    parser.add_argument("--suggest",  action="store_true",
                        help="Show suggestions for hours to block/unblock")
    parser.add_argument("--yaml",     action="store_true",
                        help="Print ready-to-paste YAML for suggested changes")
    parser.add_argument(
        "--hour-axis",
        choices=["entry_utc", "exit_utc", "entry_pt", "exit_pt"],
        default="entry_utc",
        help="Which clock to bucket hours: entry vs exit, UTC vs America/Los_Angeles",
    )
    args = parser.parse_args()

    # Load config
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    # Load trades
    print(f"[heatmap] Loading trades (last {args.days} days)...")
    axis_labels = {
        "entry_utc": "UTC hour at entry / signal",
        "exit_utc": "UTC hour at exit",
        "entry_pt": "Pacific hour at entry (fallback: exit)",
        "exit_pt": "Pacific hour at exit",
    }
    trades = load_trades(
        days_back=args.days,
        strategy_filter=args.strategy,
        hour_axis=args.hour_axis,
    )
    print(f"[heatmap] {len(trades)} trades loaded.")

    if not trades:
        print("[heatmap] No trades found. Make sure the bot has been running and logging.")
        return

    # Build stats
    stats, no_hour = build_hourly_stats(trades)
    if no_hour:
        print(f"[heatmap] Note: {no_hour} trades missing hour_utc feature (older entries).")

    blocked = load_blocked_hours(config)

    # Print heatmap
    print_heatmap(
        stats,
        blocked,
        args.min_trades,
        args.suggest,
        axis_labels[args.hour_axis],
    )

    # Print YAML suggestions if requested
    if args.yaml:
        print_suggest_yaml(stats, blocked, args.min_trades)


if __name__ == "__main__":
    main()
