"""Taken-exit settler — the exit-side counterfactual the calibration phase was missing.

Entries get a ghost log (every rejected candidate settled to its real Polymarket
outcome). Exits never had an equivalent: when a position is cut early by a
stop/TP/time-stop we record *what we got*, never *what the market eventually did*.
So we cannot answer "of the trades we stopped out, how many would have won if held?"
— which is the question blocking any data-driven stop change.

This module fills that gap. It reads EXIT events from the per-session journals
(data/paper_trades/*/entries.jsonl), looks up each market's final resolution via
the Polymarket Gamma API (reusing ResolutionTracker's proven fetch), and writes
data/calibration/trades_settled.jsonl with, per early-exited trade:

  - held_outcome       : YES/NO the market actually resolved to
  - held_win           : would OUR side have won if we'd held to resolution
  - held_pnl           : PnL we'd have realized holding to resolution
  - actual_pnl         : PnL we actually realized by exiting early
  - exit_vs_hold_pnl   : actual_pnl - held_pnl  (negative => exiting early cost us)

Resolved outcomes are cached (they never change) so re-runs are cheap.

Run:  python -m src.analysis.taken_exit_settler [--since YYYY-MM-DD] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.execution.resolution_tracker import ResolutionTracker
from src.execution.trade_journal import JOURNAL_DIR

logger = logging.getLogger(__name__)

CALIB_DIR = Path("data/calibration")
SETTLED_PATH = CALIB_DIR / "trades_settled.jsonl"
RESOLUTION_CACHE_PATH = CALIB_DIR / "_market_resolution_cache.json"

# Exit reasons that mean we left BEFORE the market resolved — the only rows whose
# hold-to-resolution counterfactual is unknown and worth settling. "RESOLVED:*"
# rows were already held to resolution, so their outcome is already recorded.
EARLY_EXIT_REASONS = frozenset(
    {"updown_stop_loss", "stop_loss", "updown_time_stop", "take_profit", "updown_expired"}
)


def _iter_exit_rows(since: Optional[str] = None) -> Iterable[Dict[str, Any]]:
    """Yield EXIT journal rows across all sessions, newest-last within each file.

    `since` filters by session-dir date suffix (test_YYYYMMDD_...) cheaply before
    opening the file.
    """
    journal_root = Path(JOURNAL_DIR)
    for session_dir in sorted(journal_root.glob("*")):
        entries = session_dir / "entries.jsonl"
        if not entries.exists():
            continue
        if since is not None:
            # session dir like test_20260531_041319 -> date token 20260531
            parts = session_dir.name.split("_")
            date_token = next((p for p in parts if len(p) == 8 and p.isdigit()), None)
            if date_token is not None:
                iso = f"{date_token[:4]}-{date_token[4:6]}-{date_token[6:8]}"
                if iso < since:
                    continue
        try:
            with open(entries, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("event") == "EXIT":
                        row["_session_id"] = session_dir.name
                        yield row
        except OSError as exc:  # pragma: no cover - best effort
            logger.warning("could not read %s: %s", entries, exc)


def _our_side_token(row: Dict[str, Any]) -> Optional[str]:
    """Which outcome token we held: 'YES' or 'NO'. None if undeterminable."""
    extra = row.get("extra") or {}
    leg = extra.get("entry_leg")
    if leg in ("YES", "NO"):
        return leg
    action = (row.get("action") or "").upper()
    if action == "BUY_YES":
        return "YES"
    if action == "BUY_NO":
        return "NO"
    # Short YES (sold YES) wins when outcome is NO.
    outcome = (row.get("outcome") or "").upper()
    if action in ("SELL", "SELL_YES") and outcome in ("YES", "NO"):
        return "NO"
    if outcome in ("YES", "NO"):
        return outcome
    return None


def _held_counterfactual(
    row: Dict[str, Any], outcome_won: str
) -> Optional[Dict[str, Any]]:
    """Compute PnL we'd have realized holding our token to resolution.

    Long a token bought at `entry_price` for `size` units: resolves to 1.0 if our
    side won (profit size*(1-entry_price)) else 0.0 (loss -size*entry_price).
    """
    side_token = _our_side_token(row)
    if side_token is None:
        return None
    try:
        entry_price = float(row.get("entry_price"))
        size = float(row.get("size"))
    except (TypeError, ValueError):
        return None
    if entry_price <= 0 or entry_price >= 1 or size <= 0:
        return None

    held_win = side_token == outcome_won
    cost_basis = entry_price * size
    held_pnl = size * (1.0 - entry_price) if held_win else -cost_basis
    held_realized_pct = (1.0 - entry_price) / entry_price if held_win else -1.0
    return {
        "our_side_token": side_token,
        "held_win": held_win,
        "held_pnl": round(held_pnl, 4),
        "held_realized_pct": round(held_realized_pct, 4),
        "cost_basis": round(cost_basis, 4),
    }


def _load_resolution_cache() -> Dict[str, Dict[str, Any]]:
    if RESOLUTION_CACHE_PATH.exists():
        try:
            return json.loads(RESOLUTION_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_resolution_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESOLUTION_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(RESOLUTION_CACHE_PATH)


def settle(
    since: Optional[str] = None,
    limit: Optional[int] = None,
    batch_size: int = 50,
) -> Dict[str, Any]:
    """Settle early-exited taken trades against real resolutions; write JSONL.

    Returns a summary dict (also printed by the CLI).
    """
    rows: List[Dict[str, Any]] = []
    seen_trade_ids = set()
    for row in _iter_exit_rows(since=since):
        if (row.get("reason") or "") not in EARLY_EXIT_REASONS:
            continue
        tid = row.get("trade_id")
        if not tid or tid in seen_trade_ids:
            continue
        if not row.get("market_id"):
            continue
        seen_trade_ids.add(tid)
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break

    cache = _load_resolution_cache()
    tracker = ResolutionTracker()

    # Fetch resolutions for markets we don't already have cached.
    needed = sorted({str(r["market_id"]) for r in rows if str(r["market_id"]) not in cache})
    logger.info("settling %d trades across %d markets (%d need fetch)",
                len(rows), len({str(r["market_id"]) for r in rows}), len(needed))
    for i in range(0, len(needed), batch_size):
        batch = needed[i : i + batch_size]
        fetched = tracker._fetch_resolutions(batch)  # noqa: SLF001 — proven Gamma lookup
        for mid, res in fetched.items():
            if res.get("resolved") and res.get("outcome_won"):
                cache[str(mid)] = {"outcome_won": res["outcome_won"]}
        # mark unresolved-this-pass markets so we don't refetch them every run within a session
    _save_resolution_cache(cache)

    settled: List[Dict[str, Any]] = []
    skipped_unresolved = 0
    for row in rows:
        mid = str(row["market_id"])
        res = cache.get(mid)
        if not res or not res.get("outcome_won"):
            skipped_unresolved += 1
            continue
        outcome_won = res["outcome_won"]
        cf = _held_counterfactual(row, outcome_won)
        if cf is None:
            continue
        extra = row.get("extra") or {}
        actual_pnl = float(row.get("pnl") or 0.0)
        rec = {
            "trade_id": row.get("trade_id"),
            "session_id": row.get("_session_id"),
            "market_id": mid,
            "ts": row.get("timestamp"),
            "strategy": row.get("strategy"),
            "window": extra.get("window_size") or extra.get("lane_window"),
            "lane_id": extra.get("lane_id"),
            "action": row.get("action"),
            "entry_price": row.get("entry_price"),
            "size": row.get("size"),
            "exit_reason": row.get("reason"),
            "actual_pnl": round(actual_pnl, 4),
            "held_outcome": outcome_won,
            **cf,
            # positive => holding would have beaten our early exit
            "hold_minus_exit_pnl": round(cf["held_pnl"] - actual_pnl, 4),
        }
        settled.append(rec)

    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTLED_PATH, "w", encoding="utf-8") as fh:
        for rec in settled:
            fh.write(json.dumps(rec) + "\n")

    summary = _summarize(settled)
    summary["n_candidate_rows"] = len(rows)
    summary["n_settled"] = len(settled)
    summary["n_skipped_unresolved"] = skipped_unresolved
    return summary


def _summarize(settled: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_reason: Dict[str, Counter] = defaultdict(Counter)
    pnl: Dict[str, Dict[str, float]] = defaultdict(lambda: {"actual": 0.0, "held": 0.0})
    for r in settled:
        reason = r["exit_reason"]
        by_reason[reason]["n"] += 1
        if r["held_win"]:
            by_reason[reason]["held_would_win"] += 1
        pnl[reason]["actual"] += r["actual_pnl"]
        pnl[reason]["held"] += r["held_pnl"]
    out = {}
    for reason, c in by_reason.items():
        n = c["n"]
        out[reason] = {
            "n": n,
            "held_would_win_pct": round(c["held_would_win"] / n * 100, 1) if n else 0.0,
            "actual_pnl": round(pnl[reason]["actual"], 2),
            "held_pnl": round(pnl[reason]["held"], 2),
            "hold_minus_exit_pnl": round(pnl[reason]["held"] - pnl[reason]["actual"], 2),
        }
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="only sessions on/after this date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="cap candidate trades (debug)")
    args = ap.parse_args()

    summary = settle(since=args.since, limit=args.limit)
    print("\n=== taken-exit settlement ===")
    print(f"candidate early-exit trades: {summary.pop('n_candidate_rows')}")
    print(f"settled (market resolved):   {summary.pop('n_settled')}")
    print(f"skipped (not yet resolved):  {summary.pop('n_skipped_unresolved')}")
    print(f"\noutput: {SETTLED_PATH}")
    print(f"\n{'exit_reason':22s} {'n':>5s} {'held-win%':>9s} {'actual$':>10s} {'held$':>10s} {'hold-exit$':>11s}")
    for reason, s in sorted(summary.items(), key=lambda kv: kv[1]["hold_minus_exit_pnl"], reverse=True):
        print(f"{reason:22s} {s['n']:5d} {s['held_would_win_pct']:8.1f}% "
              f"{s['actual_pnl']:+10.2f} {s['held_pnl']:+10.2f} {s['hold_minus_exit_pnl']:+11.2f}")


if __name__ == "__main__":
    main()
