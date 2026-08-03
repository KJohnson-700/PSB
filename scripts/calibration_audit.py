#!/usr/bin/env python3
"""Calibration audit (Script D) — is `stated_est_prob` actually honest?

Prior project finding: est_prob ~0.50 AUC, "edge came from selection, not from the
probability being sharp." This settles that quantitatively. It scores the model's
directional probability against the TRUE resolution outcome (up / down), not the
exit result:

  * reliability curve (quantile bins): mean predicted P(up) vs observed up-rate
  * Brier score + log-loss, each vs the base-rate baseline p(1-p)  -> does the
    probability beat "always predict the base rate"? If not, it adds nothing.
  * optional isotonic recalibration (sklearn) to show the achievable improvement

Label source = REAL resolution only: trades whose exit_reason is RESOLVED:YES/NO,
unioned with the settled-outcome join (trades_settled.jsonl). Early-exited trades
have no resolution label and are excluded from the resolution target. `--target
exit` instead labels on pnl>0 across all trades (bigger n) but that measures
"did est_prob predict a profitable EXIT", conflated with exit policy — a caveat,
not the model's calibration.

LIVE REALIZED only. Read-only, fail-safe. sklearn optional (guarded import).

Usage:
  python3 scripts/calibration_audit.py                    # resolution target
  python3 scripts/calibration_audit.py --field calibrated_est_prob
  python3 scripts/calibration_audit.py --by strategy --bins 5
  python3 scripts/calibration_audit.py --target exit --json
  python3 scripts/calibration_audit.py --recal            # show isotonic gain
"""
from __future__ import annotations
import argparse, json, math, sys
from collections import defaultdict
from pathlib import Path

CAL = Path(__file__).resolve().parent.parent / "data" / "calibration"
TRADES = CAL / "trades.jsonl"
SETTLED = CAL / "trades_settled.jsonl"
EPS = 1e-6


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def up_label_from_resolution(r, settle_map):
    """1 if the market resolved UP/YES, 0 if DOWN/NO, else None (no resolution)."""
    er = str(r.get("exit_reason", ""))
    if er.startswith("RESOLVED:YES"):
        return 1
    if er.startswith("RESOLVED:NO"):
        return 0
    s = settle_map.get(r.get("trade_id"))
    if s is not None:
        ho = str(s.get("held_outcome", "")).upper()
        if ho == "YES":
            return 1
        if ho == "NO":
            return 0
    return None


def load(field, target):
    trades = []
    try:
        with open(TRADES) as fh:
            for line in fh:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        print(f"[calib] no trades log at {TRADES}", file=sys.stderr); return []
    settle_map = {}
    if SETTLED.exists():
        with open(SETTLED) as fh:
            for line in fh:
                try:
                    d = json.loads(line); settle_map[d.get("trade_id")] = d
                except Exception:
                    continue
    out = []
    seen = set()
    for r in trades:
        p = _f(r, field)
        if p is None:
            continue
        if target == "exit":
            label = 1 if (_f(r, "pnl") or 0.0) > 0 else 0
        else:
            label = up_label_from_resolution(r, settle_map)
            if label is None:
                continue
        tid = r.get("trade_id")
        if tid in seen:
            continue
        seen.add(tid)
        out.append({"p": min(1 - EPS, max(EPS, p)), "y": label,
                    "strategy": r.get("strategy", "?"),
                    "lane": f"{r.get('strategy','?')}|{r.get('window','?')}|{r.get('action','?')}"})
    return out


def metrics(rows):
    n = len(rows)
    if n == 0:
        return None
    base = sum(r["y"] for r in rows) / n
    brier = sum((r["p"] - r["y"]) ** 2 for r in rows) / n
    brier_base = sum((base - r["y"]) ** 2 for r in rows) / n
    ll = -sum(r["y"] * math.log(r["p"]) + (1 - r["y"]) * math.log(1 - r["p"]) for r in rows) / n
    ll_base = -sum(r["y"] * math.log(base + EPS) + (1 - r["y"]) * math.log(1 - base + EPS)
                   for r in rows) / n
    return {"n": n, "base_rate": base, "brier": brier, "brier_base": brier_base,
            "log_loss": ll, "log_loss_base": ll_base,
            "brier_skill": (1 - brier / brier_base) if brier_base else 0.0}


def reliability(rows, bins):
    rows = sorted(rows, key=lambda r: r["p"])
    n = len(rows)
    out = []
    for b in range(bins):
        lo = b * n // bins; hi = (b + 1) * n // bins
        seg = rows[lo:hi]
        if not seg:
            continue
        mp = sum(r["p"] for r in seg) / len(seg)
        of = sum(r["y"] for r in seg) / len(seg)
        out.append({"n": len(seg), "mean_pred": mp, "obs_freq": of})
    return out


def isotonic_gain(rows):
    try:
        from sklearn.isotonic import IsotonicRegression
        import numpy as np
    except Exception:
        return None
    p = [r["p"] for r in rows]; y = [r["y"] for r in rows]
    # honest split: fit on first half (by time-order proxy = insertion order), test on second
    k = len(rows) // 2
    if k < 10:
        return None
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p[:k], y[:k])
    ph = iso.predict(p[k:])
    import numpy as np
    yt = np.array(y[k:]); ph = np.clip(np.array(ph), EPS, 1 - EPS)
    braw = float(np.mean((np.array(p[k:]) - yt) ** 2))
    biso = float(np.mean((ph - yt) ** 2))
    return {"n_test": len(yt), "brier_raw": braw, "brier_isotonic": biso}


def bar(pred, obs, width=24):
    """Tiny ASCII reliability row: 'p' marker vs 'o' marker on a 0..1 track."""
    track = ["·"] * width
    pi = min(width - 1, int(pred * width)); oi = min(width - 1, int(obs * width))
    track[pi] = "p"
    track[oi] = "O" if oi != pi else "X"
    return "".join(track)


def report_block(title, rows, bins, recal):
    m = metrics(rows)
    if not m:
        print(f"\n{title}: no labeled rows"); return
    print(f"\n{title}  ·  n={m['n']}  base-rate(up)={m['base_rate']:.3f}")
    print(f"  Brier {m['brier']:.4f}  vs base {m['brier_base']:.4f}   "
          f"skill={m['brier_skill']:+.3f}   (>0 = beats base rate)")
    print(f"  LogLoss {m['log_loss']:.4f}  vs base {m['log_loss_base']:.4f}")
    print(f"  reliability (p=predicted, O=observed up-rate; on 0.0——1.0 track):")
    for r in reliability(rows, bins):
        print(f"    pred {r['mean_pred']:.2f}  obs {r['obs_freq']:.2f}  n={r['n']:<4} |{bar(r['mean_pred'], r['obs_freq'])}|")
    if recal:
        g = isotonic_gain(rows)
        if g:
            print(f"  isotonic (out-of-sample n={g['n_test']}): Brier {g['brier_raw']:.4f} "
                  f"-> {g['brier_isotonic']:.4f}  ({'gain' if g['brier_isotonic']<g['brier_raw'] else 'no gain'})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default="stated_est_prob",
                    help="probability field (stated_est_prob|calibrated_est_prob|raw_est_prob)")
    ap.add_argument("--target", choices=["resolution", "exit"], default="resolution")
    ap.add_argument("--bins", type=int, default=5)
    ap.add_argument("--by", choices=["strategy"], default=None, help="also break out per strategy")
    ap.add_argument("--min-n", type=int, default=25, help="min n to report a per-strategy block")
    ap.add_argument("--recal", action="store_true", help="show isotonic out-of-sample gain")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load(args.field, args.target)
    if not rows:
        print("[calib] no labeled rows — resolution target needs held-to-resolution trades."); return

    if args.json:
        out = {"field": args.field, "target": args.target, "overall": metrics(rows),
               "reliability": reliability(rows, args.bins)}
        if args.recal:
            out["isotonic"] = isotonic_gain(rows)
        if args.by == "strategy":
            byk = defaultdict(list)
            for r in rows:
                byk[r["strategy"]].append(r)
            out["by_strategy"] = {k: metrics(v) for k, v in byk.items() if len(v) >= args.min_n}
        print(json.dumps(out, indent=2, default=float)); return

    tgt = ("RESOLUTION outcome (up/down)" if args.target == "resolution"
           else "EXIT win (pnl>0) — conflated with exit policy, big-n proxy only")
    print(f"\nCALIBRATION AUDIT  ·  field={args.field}  ·  target={tgt}")
    report_block("OVERALL", rows, args.bins, args.recal)
    if args.by == "strategy":
        byk = defaultdict(list)
        for r in rows:
            byk[r["strategy"]].append(r)
        for k in sorted(byk, key=lambda k: -len(byk[k])):
            if len(byk[k]) >= args.min_n:
                report_block(f"[{k}]", byk[k], args.bins, args.recal)
    print("\n  read: if Brier skill <= 0, the probability is no better than the base rate —")
    print("        the edge is selection/exit, not a sharp probability. A rising obs-rate")
    print("        across bins = the model at least ranks direction, even if miscalibrated.\n")


if __name__ == "__main__":
    main()
