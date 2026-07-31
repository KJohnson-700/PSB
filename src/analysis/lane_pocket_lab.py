"""Lane Pocket Lab — one reusable framework to score EVERY strategy lane the same way.

Operator 2026-07-30. Replaces one-off per-lane funnel reports. Read-only: consumes the
telemetry the bot already writes; changes no config and no trading behavior.

Four questions, one framework:
  1. MATRIX  (lane_pocket_matrix.jsonl) — which pockets EXIST, and does their edge survive
     the move from old fake-fill paper to live-like paper? (old_paper vs live_like_paper).
  2. FUNNEL  (lane_funnel.jsonl)        — why a pocket is NOT firing (which gate rejects it).
  3. EXITLAB (lane_exit_lab.jsonl)      — why a pocket LOSES after entering (MFE/MAE, green-
     then-stopped-red, exit reasons, exit book quality).
  4. PROMOTION rule                     — classify each pocket into an action bucket.

A "pocket" = (strategy, asset, window, side, entry_family, lane_regime). All rows share the
operator's schema so the three files are one dataset seen through three lenses.

Sources (all existing):
  data/calibration/trades.jsonl               closed trades (matrix + exit lab)
  data/calibration/rejected_candidates.jsonl  per-candidate rejects (funnel)
  data/calibration/window_watch.jsonl         lane_entry_window "too-early" rejects (funnel)

Schema field mapping (telemetry name -> operator name):
  lane_family        -> entry_family
  regime_tag_bucket  -> lane_regime
  mfe_pct / mae_pct  -> median_mfe / median_mae
  stated_edge        -> edge_median
  entry_fee_usdc + fill_fee_usdc -> fee_total (live-like only)
  entry_sim_fill_ratio | exit_fill_ratio -> fill_ratio_median (live-like only)

CLI:
  python -m src.analysis.lane_pocket_lab            # full run, writes 3 jsonl + prints matrix
  python -m src.analysis.lane_pocket_lab --lanes bitcoin:5m xrp_macro:5m,15m,1h  # seed view
  python -m src.analysis.lane_pocket_lab --min-closed 15 --json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent.parent
_CAL = _ROOT / "data" / "calibration"
TRADES = _CAL / "trades.jsonl"
REJECTS = _CAL / "rejected_candidates.jsonl"
WINDOW_WATCH = _CAL / "window_watch.jsonl"
OUT_MATRIX = _CAL / "lane_pocket_matrix.jsonl"
OUT_FUNNEL = _CAL / "lane_funnel.jsonl"
OUT_EXITLAB = _CAL / "lane_exit_lab.jsonl"

_ASSET = {
    "bitcoin": "BTC", "sol_macro": "SOL", "eth_macro": "ETH", "xrp_macro": "XRP",
    "bnb_macro": "BNB", "doge_macro": "DOGE", "hype_macro": "HYPE",
}

# A trade is live-like paper (realistic book-walk fills + fee drag) ONLY if it carries a field
# from the 2026-07-30 fill/fee rework. NOTE: ws_price_age_ms is deliberately EXCLUDED — it is a
# price-freshness field present since ~07-20 on real-WS-but-still-fake-fill trades, so counting it
# misclassifies ~2250 old-paper rows as live-like. The true realistic-execution markers are the
# fill-simulator + fee-drag + execution-adjusted fields, all of which begin 2026-07-30.
_LIVE_LIKE_MARKERS = (
    "execution_adjusted_pnl", "fill_fee_usdc", "entry_fee_usdc",
    "entry_sim_fill_ratio", "exit_best_bid", "entry_spread", "fill_slippage_pct",
)

# --- promotion-rule thresholds (operator Phase 5; module constants so they are tunable) ---
PROMOTE_MIN_CLOSED = 15       # live-like closed trades required to even consider promotion
PROMOTE_MIN_WR = 0.50         # OR positive avg_pnl if WR below this
GREEN_MFE = 0.08              # "went meaningfully green" = peak >= +8%
DO_NOT_RESTORE_OLD_PNL = 10.0  # old-paper pnl that looks like edge...
DO_NOT_RESTORE_LIVE_WR = 0.40  # ...but live-like WR below this (or negative pnl) = fake edge
EXIT_FIX_GREEN_RATE = 0.40    # >=40% of losers went green first = exit problem, not entry
ENTRY_CHOKE_MIN_REJECTS = 20  # heavy rejects...
ENTRY_CHOKE_MAX_CLOSED = 3    # ...with almost no fills = over-gated
WATCH_MIN_CLOSED = 3          # below this in live-like = not enough to say anything


def _iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _side(action: Optional[str], side: Optional[str] = None) -> str:
    a = str(action or "").upper()
    if a == "BUY_YES":
        return "UP"
    if a == "BUY_NO":
        return "DOWN"
    s = str(side or "").upper()
    return {"LONG": "UP", "UP": "UP", "SHORT": "DOWN", "DOWN": "DOWN"}.get(s, "?")


def _asset(strategy: Optional[str]) -> str:
    s = str(strategy or "")
    return _ASSET.get(s, s.replace("_macro", "").upper() or "?")


def _mode(row: dict) -> str:
    # Explicit stamp (main.py, 2026-07-30) wins: a real LIVE trade carries the same execution
    # fields as live-like paper, so only the stamp can separate them. Paper rows (stamped or old)
    # still split old_paper vs live_like_paper by whether realistic-fill fields are present.
    if str(row.get("mode")) == "live":
        return "live"
    return "live_like_paper" if any(row.get(k) is not None for k in _LIVE_LIKE_MARKERS) else "old_paper"


def _pocket_key(row: dict) -> Tuple[str, str, str, str]:
    """(strategy, window, side, entry_family). Coarse enough to hold a sample, fine enough to
    match the operator's 'btc 5m up drift' examples. lane_regime tracked as a reported field."""
    return (
        str(row.get("strategy") or "?"),
        str(row.get("window") or "?"),
        _side(row.get("action"), row.get("side")),
        str(row.get("lane_family") or "?"),
    )


def _fnum(vals: Iterable[Any]) -> List[float]:
    out = []
    for v in vals:
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _median(vals: List[float]) -> Optional[float]:
    return round(st.median(vals), 4) if vals else None


# ---------------------------------------------------------------------------
# Load + group
# ---------------------------------------------------------------------------
def _load_trades() -> List[dict]:
    return list(_iter_jsonl(TRADES))


def _load_rejects() -> List[dict]:
    rows = list(_iter_jsonl(REJECTS))
    for r in _iter_jsonl(WINDOW_WATCH):  # fold too-early window rejects into the funnel
        r.setdefault("reason", "lane_entry_window")
        rows.append(r)
    return rows


def _funnel_by_pocket(rejects: List[dict]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Funnel keys on (strategy, window, side) — rejects predate the accepted lane_family, so a
    coarser key is the honest join. Returns reject counts + top reasons per (strat,window,side)."""
    agg: Dict[Tuple[str, str, str], Counter] = defaultdict(Counter)
    for r in rejects:
        strat = str(r.get("strategy") or r.get("asset") or "?")
        # window_watch rows use `asset` (BTC) not strategy; normalize back to strategy key space
        if strat.upper() in _ASSET.values():
            strat = {v: k for k, v in _ASSET.items()}.get(strat.upper(), strat)
        key = (strat, str(r.get("window") or "?"), _side(r.get("action"), r.get("side")))
        agg[key][str(r.get("reason") or "?")] += 1
    out = {}
    for key, c in agg.items():
        out[key] = {"reject_count": sum(c.values()), "top_reject_reasons": dict(c.most_common(6))}
    return out


def _stats_for_group(rows: List[dict]) -> Dict[str, Any]:
    closed = rows
    n = len(closed)
    pnls = _fnum(r.get("pnl") for r in closed)
    wins = sum(1 for r in closed if isinstance(r.get("pnl"), (int, float)) and r["pnl"] > 0)
    fees = _fnum(
        (r.get("entry_fee_usdc") or 0) + (r.get("fill_fee_usdc") or 0)
        for r in closed if (r.get("entry_fee_usdc") is not None or r.get("fill_fee_usdc") is not None)
    )
    fill_ratios = _fnum(
        r.get("entry_sim_fill_ratio") if r.get("entry_sim_fill_ratio") is not None else r.get("exit_fill_ratio")
        for r in closed
    )
    mfes = _fnum(r.get("mfe_pct") for r in closed)
    green_losers = [r for r in closed
                    if isinstance(r.get("mfe_pct"), (int, float)) and r["mfe_pct"] >= GREEN_MFE
                    and isinstance(r.get("pnl"), (int, float)) and r["pnl"] < 0]
    stop_after_green = [r for r in green_losers if "stop" in str(r.get("exit_reason") or "").lower()]
    exit_reasons = Counter(str(r.get("exit_reason") or "?") for r in closed)
    return {
        "entry_count": n,          # closed-trade source: entries that resolved (see module docstring)
        "closed_count": n,
        "pnl": round(sum(pnls), 2) if pnls else 0.0,
        "win_rate": round(wins / n, 3) if n else None,
        "avg_pnl": round(sum(pnls) / n, 3) if n else None,
        "median_mfe": _median(mfes),
        "median_mae": _median(_fnum(r.get("mae_pct") for r in closed)),
        "entry_price_median": _median(_fnum(r.get("entry_price") for r in closed)),
        "edge_median": _median(_fnum(r.get("stated_edge") for r in closed)),
        "fill_ratio_median": _median(fill_ratios),
        "entry_spread_median": _median(_fnum(r.get("entry_spread") for r in closed)),
        "fee_total": round(sum(fees), 4) if fees else 0.0,
        "top_exit_reasons": dict(exit_reasons.most_common(5)),
        "green_then_red_rate": round(len(green_losers) / n, 3) if n else None,
        "stop_after_green": len(stop_after_green),
        "sessions": len({r.get("session_id") for r in closed}),
    }


def _classify(pocket_modes: Dict[str, Dict[str, Any]], funnel: Dict[str, Any]) -> str:
    """One verdict per pocket, from old vs live-like edge + funnel + exit shape."""
    old = pocket_modes.get("old_paper")
    live = pocket_modes.get("live_like_paper")
    reject_ct = (funnel or {}).get("reject_count", 0)

    live_n = live["closed_count"] if live else 0
    old_pnl = old["pnl"] if old else 0.0
    live_pnl = live["pnl"] if live else 0.0
    live_wr = (live or {}).get("win_rate")
    live_avg = (live or {}).get("avg_pnl")
    live_mfe = (live or {}).get("median_mfe")
    green_rate = (live or {}).get("green_then_red_rate")

    # 1. Old edge that does NOT survive live-like execution.
    if old and old_pnl >= DO_NOT_RESTORE_OLD_PNL and live_n >= WATCH_MIN_CLOSED:
        if live_pnl < 0 or (live_wr is not None and live_wr < DO_NOT_RESTORE_LIVE_WR):
            return "do_not_restore"

    # 2. Enters fine (goes green) but loses on the exit.
    if live_n >= WATCH_MIN_CLOSED and live_pnl < 0 and live_mfe is not None and live_mfe >= GREEN_MFE:
        if green_rate is not None and green_rate >= EXIT_FIX_GREEN_RATE:
            return "exit_fix_candidate"

    # 3. Barely fires while a gate rejects heavily = over-gated.
    if live_n <= ENTRY_CHOKE_MAX_CLOSED and reject_ct >= ENTRY_CHOKE_MIN_REJECTS:
        return "entry_gate_choked"

    # 4. Promotable on live-like evidence.
    if live_n >= PROMOTE_MIN_CLOSED and live_pnl > 0 and (
        (live_wr is not None and live_wr >= PROMOTE_MIN_WR) or (live_avg is not None and live_avg > 0)
    ) and (green_rate is None or green_rate < EXIT_FIX_GREEN_RATE):
        return "promote_candidate"

    # 5. Have live-like data but not enough to rule.
    if live_n >= WATCH_MIN_CLOSED:
        return "live_like_watch"

    # 6. Only old paper — cannot judge under realistic execution.
    if old and not live:
        return "old_paper_only"
    return "live_like_watch"


def build() -> Dict[str, List[dict]]:
    trades = _load_trades()
    funnel = _funnel_by_pocket(_load_rejects())

    # group closed trades by (pocket, mode)
    groups: Dict[Tuple[str, str, str, str], Dict[str, List[dict]]] = defaultdict(lambda: defaultdict(list))
    regime: Dict[Tuple[str, str, str, str], Counter] = defaultdict(Counter)
    for r in trades:
        pk = _pocket_key(r)
        groups[pk][_mode(r)].append(r)
        regime[pk][str(r.get("regime_tag_bucket") or "?")] += 1

    matrix_rows: List[dict] = []
    exit_rows: List[dict] = []
    funnel_rows: List[dict] = []
    seen_funnel_keys = set()

    for pk, by_mode in sorted(groups.items()):
        strategy, window, side, entry_family = pk
        lane_regime = regime[pk].most_common(1)[0][0] if regime[pk] else "?"
        fkey = (strategy, window, side)
        fn = funnel.get(fkey, {})
        mode_stats = {mode: _stats_for_group(rows) for mode, rows in by_mode.items()}
        classification = _classify(mode_stats, fn)

        for mode, s in mode_stats.items():
            base = {
                "strategy": strategy, "asset": _asset(strategy), "window": window, "side": side,
                "entry_family": entry_family, "lane_regime": lane_regime,
                "lane_id": None, "session_id": "*", "mode": mode,
                "entry_count": s["entry_count"], "closed_count": s["closed_count"],
                "pnl": s["pnl"], "win_rate": s["win_rate"], "avg_pnl": s["avg_pnl"],
                "median_mfe": s["median_mfe"], "median_mae": s["median_mae"],
                "entry_price_median": s["entry_price_median"], "edge_median": s["edge_median"],
                "fill_ratio_median": s["fill_ratio_median"], "entry_spread_median": s["entry_spread_median"],
                "fee_total": s["fee_total"],
                "top_exit_reasons": s["top_exit_reasons"],
                "top_reject_reasons": fn.get("top_reject_reasons", {}),
                "green_then_red_rate": s["green_then_red_rate"],
                "stop_after_green": s["stop_after_green"],
                "classification": classification, "sessions": s["sessions"],
            }
            matrix_rows.append(base)
            exit_rows.append({**base, "reject_count": fn.get("reject_count", 0)})
        seen_funnel_keys.add(fkey)

    # funnel rows: every (strat,window,side) that has rejects, even pockets that never traded
    for fkey, fn in sorted(funnel.items()):
        strategy, window, side = fkey
        funnel_rows.append({
            "strategy": strategy, "asset": _asset(strategy), "window": window, "side": side,
            "entry_family": "*", "lane_regime": "*", "lane_id": None, "session_id": "*",
            "mode": "reject_stream", "reject_count": fn["reject_count"],
            "top_reject_reasons": fn["top_reject_reasons"],
            "traded": fkey in seen_funnel_keys,
        })

    return {"matrix": matrix_rows, "funnel": funnel_rows, "exit_lab": exit_rows}


def _write(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def _lane_filter(rows: List[dict], lanes: Optional[List[Tuple[str, Optional[str]]]]) -> List[dict]:
    if not lanes:
        return rows
    def ok(r):
        for strat, win in lanes:
            if r.get("strategy") == strat and (win is None or r.get("window") == win):
                return True
        return False
    return [r for r in rows if ok(r)]


def _fmt_matrix(rows: List[dict]) -> str:
    L = ["=== LANE POCKET MATRIX (old_paper vs live_like_paper) ==="]
    L.append(f"{'pocket':<40}{'mode':<16}{'n':>4}{'pnl':>9}{'wr':>6}{'mfe':>7}{'mae':>7}{'g>r':>6}  class")
    def sk(r): return (r["strategy"], r["window"], r["side"], r["entry_family"], r["mode"])
    for r in sorted(rows, key=sk):
        pocket = f"{r['asset']} {r['window']} {r['side']} {r['entry_family']}"[:39]
        wr = f"{r['win_rate']*100:.0f}%" if r["win_rate"] is not None else "-"
        mfe = f"{r['median_mfe']*100:.0f}%" if r["median_mfe"] is not None else "-"
        mae = f"{r['median_mae']*100:.0f}%" if r["median_mae"] is not None else "-"
        gr = r.get("green_then_red_rate")
        grs = f"{gr*100:.0f}%" if gr is not None else "-"
        L.append(f"{pocket:<40}{r['mode']:<16}{r['closed_count']:>4}{r['pnl']:>9.2f}{wr:>6}{mfe:>7}{mae:>7}{grs:>6}  {r['classification']}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Lane Pocket Lab")
    ap.add_argument("--lanes", nargs="*", default=None,
                    help="filter e.g. bitcoin:5m xrp_macro:5m,15m,1h  (strategy or strategy:win[,win])")
    ap.add_argument("--json", action="store_true", help="print matrix rows as JSON")
    ap.add_argument("--no-write", action="store_true", help="do not write the 3 jsonl files")
    args = ap.parse_args()

    lanes = None
    if args.lanes:
        lanes = []
        for tok in args.lanes:
            if ":" in tok:
                strat, wins = tok.split(":", 1)
                for w in wins.split(","):
                    lanes.append((strat, w))
            else:
                lanes.append((tok, None))

    out = build()
    if not args.no_write:
        _write(OUT_MATRIX, out["matrix"])
        _write(OUT_FUNNEL, out["funnel"])
        _write(OUT_EXITLAB, out["exit_lab"])

    view = _lane_filter(out["matrix"], lanes)
    if args.json:
        print(json.dumps(view, indent=2))
    else:
        print(_fmt_matrix(view))
        print(f"\nwrote: {OUT_MATRIX.name} ({len(out['matrix'])})  "
              f"{OUT_FUNNEL.name} ({len(out['funnel'])})  {OUT_EXITLAB.name} ({len(out['exit_lab'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
