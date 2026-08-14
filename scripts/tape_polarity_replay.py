#!/usr/bin/env python3
"""tape_polarity_replay — the SHADOW-BEFORE-LIVE proof for a self-flipping realized adapter.

Question this answers (task #106): tape_map runs ~27-29% right now (INVERTED, not coinflip).
Would a REALIZED adapter that flips tape_map's side when its recent hit-rate is inverted
actually beat raw tape_map — or is the inversion just noise that reverts unpredictably (in
which case a self-flip chases its tail)? We must know BEFORE wiring the live side path.

Method (STRICT no-lookahead):
  For each asset, in time order, at call i we compute a TRAILING hit-rate over the last W
  calls WHOSE OUTCOME WAS ALREADY KNOWN at time ts_i (i.e. call_ts + horizon <= ts_i). From
  that trailing hit-rate we set a polarity:
      INVERTED  if trailing_acc < 0.5 - margin  (and >= MIN_OBS scored calls seen)
      NORMAL    if trailing_acc > 0.5 + margin
      NEUTRAL   otherwise  (adapter leaves the call as-is)
  Then the adapter's direction for call i = FLIP(raw) if INVERTED else raw. We score BOTH raw
  and adapter against call i's own realized forward sign. Polarity is derived only from the
  PAST, so this is exactly what a live lagging adapter could have done.

  A self-flip is JUSTIFIED only if adapter_acc > raw_acc by a real margin across assets AND
  the polarity state is persistent (INVERTED runs long enough to exploit). Otherwise: DO NOT
  wire it — report that the inversion is noise.

Reads:  data/calibration/tape_map.jsonl   (ts, asset, direction, confidence, price)
Prints: per-asset + overall raw vs adapter accuracy, polarity-state dwell, and a verdict.
Read-only. Run:  .venv/bin/python scripts/tape_polarity_replay.py [--horizon 15] [--window 20] [--margin 0.05]
"""
from __future__ import annotations
import argparse
import bisect
import json
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / "data" / "calibration" / "tape_map.jsonl"
FLIP = {"UP": "DOWN", "DOWN": "UP"}


def _load():
    per = defaultdict(list)
    for line in open(MAP_PATH):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts, px = d.get("ts"), d.get("price")
        if not isinstance(ts, (int, float)) or not isinstance(px, (int, float)) or px <= 0:
            continue
        per[d.get("asset")].append((ts, str(d.get("direction") or "").upper(),
                                    float(d.get("confidence", 0.0) or 0.0), float(px)))
    for a in per:
        per[a].sort()
    return per


def _future_sign(rows, ts_list, i, horizon_s, tol_s):
    """Realized sign of the move `horizon` later (nearest snapshot within tol). None if series ends."""
    target = rows[i][0] + horizon_s
    j = bisect.bisect_left(ts_list, target)
    best = None
    for k in (j - 1, j, j + 1):
        if 0 <= k < len(rows):
            dt = abs(rows[k][0] - target)
            if dt <= tol_s and (best is None or dt < best[0]):
                best = (dt, rows[k][3])
    if best is None:
        return None
    move = best[1] - rows[i][3]
    return 1 if move > 0 else (-1 if move < 0 else 0)


def replay(per, horizon_s, window, margin, min_obs, conf_gate):
    tol_s = max(45.0, horizon_s * 0.25)
    per_asset = {}
    ov_raw = [0, 0]; ov_adj = [0, 0]
    state_dwell = defaultdict(int)
    for asset, rows in per.items():
        ts_list = [r[0] for r in rows]
        # precompute realized sign for every call (None if unscorable)
        signs = [_future_sign(rows, ts_list, i, horizon_s, tol_s) for i in range(len(rows))]
        # trailing buffer of (dir, sign) for calls whose OUTCOME was known by the current ts
        trail = deque(maxlen=window)
        settle_ptr = 0   # next call index whose outcome may have settled
        raw = [0, 0]; adj = [0, 0]
        for i, (ts, dr, conf, px) in enumerate(rows):
            # admit into trailing buffer every past call j whose outcome settled by ts
            while settle_ptr < i:
                tj, drj, cj, pxj = rows[settle_ptr]
                if tj + horizon_s <= ts:
                    if drj in ("UP", "DOWN") and signs[settle_ptr] is not None:
                        trail.append((drj, signs[settle_ptr]))
                    settle_ptr += 1
                else:
                    break
            # polarity from the past only
            hits = sum(1 for d, s in trail if (s > 0) == (d == "UP") and s != 0)
            obs = sum(1 for d, s in trail if s != 0)
            if obs >= min_obs:
                acc = hits / obs
                state = "INVERTED" if acc < 0.5 - margin else ("NORMAL" if acc > 0.5 + margin else "NEUTRAL")
            else:
                state = "NEUTRAL"
            state_dwell[state] += 1
            # score this call (UP/DOWN, scorable, conf gate) under raw vs adapter
            if dr in ("UP", "DOWN") and signs[i] is not None and signs[i] != 0 and conf >= conf_gate:
                raw_ok = (signs[i] > 0) == (dr == "UP")
                use = FLIP[dr] if state == "INVERTED" else dr
                adj_ok = (signs[i] > 0) == (use == "UP")
                raw[0] += raw_ok; raw[1] += 1
                adj[0] += adj_ok; adj[1] += 1
        per_asset[asset] = (raw, adj)
        ov_raw[0] += raw[0]; ov_raw[1] += raw[1]
        ov_adj[0] += adj[0]; ov_adj[1] += adj[1]
    return per_asset, ov_raw, ov_adj, state_dwell


def pct(cr):
    return (100.0 * cr[0] / cr[1]) if cr[1] else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=15, help="forward minutes to score (default 15)")
    ap.add_argument("--window", type=int, default=20, help="trailing scored-calls window for polarity")
    ap.add_argument("--margin", type=float, default=0.05, help="dead-band around 0.5 for NEUTRAL")
    ap.add_argument("--min-obs", type=int, default=8, help="min settled calls in window before a polarity call")
    ap.add_argument("--conf-gate", type=float, default=0.6)
    args = ap.parse_args()

    per = _load()
    per_asset, ov_raw, ov_adj, dwell = replay(
        per, args.horizon * 60, args.window, args.margin, args.min_obs, args.conf_gate)

    print("=" * 72)
    print(f"TAPE-POLARITY SELF-FLIP REPLAY  (horizon={args.horizon}m window={args.window} "
          f"margin={args.margin} min_obs={args.min_obs} conf>={args.conf_gate})")
    print("=" * 72)
    print(f'{"asset":<10} {"raw%":>7} {"adapter%":>9} {"n":>5}  {"delta":>7}')
    for a in sorted(per_asset):
        raw, adj = per_asset[a]
        d = pct(adj) - pct(raw)
        print(f'{a.replace("_macro",""):<10} {pct(raw):>6.1f}% {pct(adj):>8.1f}% {raw[1]:>5}  {d:>+6.1f}')
    print("-" * 72)
    delta = pct(ov_adj) - pct(ov_raw)
    print(f'{"OVERALL":<10} {pct(ov_raw):>6.1f}% {pct(ov_adj):>8.1f}% {ov_raw[1]:>5}  {delta:>+6.1f}')
    tot_states = sum(dwell.values()) or 1
    print("\npolarity-state dwell: " + "  ".join(
        f"{k}={v}({100*v/tot_states:.0f}%)" for k, v in sorted(dwell.items())))
    print("\nVERDICT:")
    if ov_raw[1] < 30:
        print("  INSUFFICIENT n (<30 scored calls) — inconclusive, keep accumulating.")
    elif delta >= 4.0 and pct(ov_adj) > 50:
        print(f"  ADAPTER HELPS (+{delta:.1f}pts, adapter>{50}%) — self-flip is justified; wire it (shadow->live).")
    elif delta >= 4.0:
        print(f"  adapter improves raw (+{delta:.1f}pts) but still <=50% — flip helps yet edge weak; wire in SHADOW only.")
    else:
        print(f"  ADAPTER DOES NOT HELP ({delta:+.1f}pts) — inversion is NOT persistently exploitable; DO NOT wire.")


if __name__ == "__main__":
    main()
