#!/usr/bin/env python3
"""CEX->PM repricing-lag SHADOW (observe-only, 2026-08-05).

Tests the ONE signal our HF analysis said could actually beat the ~coinflip on short-window PM
up/down: does BINANCE's just-happened move predict the Polymarket 5m/15m outcome BEFORE the PM mid
catches up (the CEX->PM repricing lag)? Direction *prediction* is efficiently priced; TIMING/lag is
the structural edge.

Design (zero trading side effects, no bot code change, no restart):
  * Piggyback on data/calibration/rejected_candidates.jsonl (every scanned candidate already carries
    market_id + yes_price = the PM mid at scan + ts + asset + side).
  * Per ~30s cycle, fetch the recent Binance move ONCE per asset (klines, cheap REST) and attach it to
    that cycle's fresh candidates -> one lag row per (market_id, ts).
  * Outcomes are joined LATER from rejected_candidates_settled.jsonl (market_id -> outcome) by the
    analysis script; nothing here needs to settle.

Read-out (separate analysis): does binance_dir predict the outcome, and — the tradeable part — does
it beat the PM mid (i.e. is the mid mispriced when it lags a fresh Binance move)? Edge must clear fees.

MVP caveat: the Binance move is the current ~2-min move at daemon-cycle time, ~<=30s after the
candidate was logged, so it's approximate (good enough to detect whether a signal EXISTS; refine to
klines ending at candidate.ts if it shows promise).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.analysis.model_factory import _fetch_klines, _BINANCE_SYMBOL  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAND = os.path.join(_REPO, "data/calibration/rejected_candidates.jsonl")
OUT = os.path.join(_REPO, "data/calibration/cex_pm_lag_shadow.jsonl")
CYCLE_S = 30
LOOKBACK_MIN = 2          # Binance move over the last ~2 minutes
MOVE_EPS = 0.02           # % move below this = FLAT (noise)
LAG_MOVE_MIN = 0.05       # a "meaningful" Binance move for the mid-lags flag
MID_NEAR_EVEN = 0.08      # mid within this of 0.50 = "hasn't priced the move yet"


def binance_move_pct(symbol):
    """% close move over the last LOOKBACK_MIN 1m candles. None on failure."""
    kl = _fetch_klines(symbol, "1m", limit=LOOKBACK_MIN + 2)
    if not kl or len(kl) < LOOKBACK_MIN + 1:
        return None
    closes = [c for c, _ in kl]
    base = closes[-(LOOKBACK_MIN + 1)]
    last = closes[-1]
    if not base:
        return None
    return round(100.0 * (last - base) / base, 4)


def asset_of(strategy):
    a = str(strategy or "").replace("_macro", "").upper()
    return "BTC" if a == "BITCOIN" else a


def make_row(cand, move):
    mid = cand.get("yes_price")
    mkt = cand.get("market_id")
    win = str(cand.get("window"))
    if mkt is None or mid is None or win not in ("5m", "15m"):
        return None
    try:
        mid = float(mid)
    except (TypeError, ValueError):
        return None
    bdir = "UP" if move > MOVE_EPS else ("DOWN" if move < -MOVE_EPS else "FLAT")
    pm_up = mid > 0.5
    return {
        "ts": cand.get("ts"),
        "market_id": mkt,
        "asset": asset_of(cand.get("strategy")),
        "window": win,
        "pm_mid": mid,
        "pm_implied_up": pm_up,
        "binance_move_pct": move,
        "binance_dir": bdir,
        # the lag hypothesis: Binance moved meaningfully but the PM mid is still ~even
        # (hasn't repriced) => betting the Binance direction should have edge if PM catches up.
        "mid_lags_binance": bool(abs(move) >= LAG_MOVE_MIN and abs(mid - 0.5) <= MID_NEAR_EVEN),
        # does the fresh Binance dir disagree with what the mid implies? (potential mispricing)
        "binance_vs_mid_disagree": bool(bdir != "FLAT" and (bdir == "UP") != pm_up),
        "side": cand.get("action"),
        "est_prob": cand.get("est_prob") or cand.get("est_prob_up"),
        "shadow_kind": "cex_pm_lag",
    }


def main():
    seen = set()
    pos = os.path.getsize(CAND) if os.path.exists(CAND) else 0
    print(f"[cex_pm_lag_shadow] watching {CAND} from offset {pos}; out={OUT}", flush=True)
    while True:
        try:
            if not os.path.exists(CAND):
                time.sleep(CYCLE_S)
                continue
            sz = os.path.getsize(CAND)
            if sz < pos:            # log rotated
                pos = 0
            if sz > pos:
                with open(CAND) as f:
                    f.seek(pos)
                    lines = f.readlines()
                    pos = f.tell()
                fresh = []
                assets = set()
                for ln in lines:
                    ln = ln.strip()
                    if not ln.startswith("{"):
                        continue
                    try:
                        c = json.loads(ln)
                    except Exception:
                        continue
                    if str(c.get("window")) not in ("5m", "15m"):
                        continue
                    key = f"{c.get('market_id')}|{c.get('ts')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh.append(c)
                    assets.add(asset_of(c.get("strategy")))
                if fresh:
                    # one Binance fetch per asset per cycle
                    moves = {}
                    for a in assets:
                        sym = _BINANCE_SYMBOL.get(a)
                        moves[a] = binance_move_pct(sym) if sym else None
                    rows = []
                    for c in fresh:
                        mv = moves.get(asset_of(c.get("strategy")))
                        if mv is None:
                            continue
                        r = make_row(c, mv)
                        if r:
                            rows.append(r)
                    if rows:
                        with open(OUT, "a") as o:
                            for r in rows:
                                o.write(json.dumps(r) + "\n")
                        print(f"[cex_pm_lag_shadow] logged {len(rows)} rows "
                              f"(assets={sorted(assets)})", flush=True)
                    # bound memory
                    if len(seen) > 200000:
                        seen = set(list(seen)[-50000:])
        except Exception as e:
            print(f"[cex_pm_lag_shadow] err: {e}", flush=True)
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    main()
