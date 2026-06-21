#!/usr/bin/env python3
"""Exit-quality monitor — catches the failure mode we missed for two days:
stops cutting winners + payoff-ratio decay.

Reads data/calibration/trades.jsonl (taken, settled trades) and reports, over the
last N closed trades:
  - stop_loss share of exits           (stops dominating?)
  - take_profit share
  - payoff ratio = avg_win / avg_loss  (baseline 1.68)
  - %% of stop-losses that had gone GREEN (MFE > +2%) before stopping  <-- THE tell
  - avg peak (MFE) on stopped trades

Emits one compact line for the watch + a verdict token (OK/WARN/ALERT) so
vps_watch.sh can fold it into the PRIORITY line and Discord alert. Pure stdlib,
read-only, fail-safe.

Usage: python3 scripts/exit_quality_audit.py [--n 200] [--json]
"""
from __future__ import annotations
import argparse, json, sys, statistics as st
from pathlib import Path

TRADES = Path(__file__).resolve().parent.parent / "data" / "calibration" / "trades.jsonl"

# thresholds (derived from the 2026-06-21 miss)
STOP_SHARE_WARN = 0.50          # stops should not dominate exits
GREEN_STOP_ALERT = 0.40        # >40% of stops were green before stopping = cutting winners
PAYOFF_WARN = 1.50             # baseline 1.68
PAYOFF_ALERT = 1.30
MFE_GREEN = 0.02              # "went green" threshold
MIN_N = 20                    # need this many closed trades to judge


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def audit(path: Path, n: int) -> dict:
    try:
        rows = [json.loads(l) for l in open(path) if l.strip()][-n:]
    except OSError:
        return {"verdict": "OK", "reason": "no_trades_file", "n": 0}
    closed = [r for r in rows if r.get("exit_reason")]
    if len(closed) < MIN_N:
        return {"verdict": "OK", "reason": f"thin(n={len(closed)})", "n": len(closed)}

    n_tot = len(closed)
    stops = [r for r in closed if r.get("exit_reason") in ("updown_stop_loss", "updown_time_stop")]
    tps = [r for r in closed if r.get("exit_reason") == "take_profit"]
    stop_share = len(stops) / n_tot
    tp_share = len(tps) / n_tot

    wins = [_f(r, "pnl") for r in closed if r.get("win") and _f(r, "pnl") is not None]
    loss = [abs(_f(r, "pnl")) for r in closed if not r.get("win") and _f(r, "pnl") is not None]
    payoff = (st.mean(wins) if wins else 0) / (st.mean(loss) if loss else 1e-9)

    green_stops = [r for r in stops if (_f(r, "mfe_pct") or 0) > MFE_GREEN]
    green_stop_frac = (len(green_stops) / len(stops)) if stops else 0.0
    avg_stop_mfe = st.mean([_f(r, "mfe_pct") or 0 for r in stops]) if stops else 0.0

    verdict, reasons = "OK", []
    if green_stop_frac > GREEN_STOP_ALERT:
        verdict = "ALERT"
        reasons.append(f"stops_cutting_winners({green_stop_frac:.0%}>{GREEN_STOP_ALERT:.0%},avg_peak+{avg_stop_mfe:.0%})")
    if payoff < PAYOFF_ALERT:
        verdict = "ALERT"; reasons.append(f"payoff_low({payoff:.2f}<{PAYOFF_ALERT})")
    elif payoff < PAYOFF_WARN and verdict != "ALERT":
        verdict = "WARN"; reasons.append(f"payoff_soft({payoff:.2f}<{PAYOFF_WARN})")
    if stop_share > STOP_SHARE_WARN and verdict == "OK":
        verdict = "WARN"; reasons.append(f"stop_heavy({stop_share:.0%})")

    return {
        "verdict": verdict, "reason": ",".join(reasons) or "ok", "n": n_tot,
        "stop_share": round(stop_share, 3), "tp_share": round(tp_share, 3),
        "payoff": round(payoff, 2), "green_stop_frac": round(green_stop_frac, 3),
        "avg_stop_mfe": round(avg_stop_mfe, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--watch", action="store_true",
                    help="space-separated tokens for vps_watch.sh: verdict stop_share payoff green_stop_frac reason")
    ap.add_argument("--path", default=str(TRADES))
    a = ap.parse_args()
    try:
        res = audit(Path(a.path), a.n)
    except Exception as e:  # never break the watch
        res = {"verdict": "OK", "reason": f"audit_error:{type(e).__name__}", "n": 0}
    if a.watch:
        reason = str(res.get("reason", "ok") or "ok").replace(" ", "_")
        print(res["verdict"], res.get("stop_share", "-"), res.get("payoff", "-"),
              res.get("green_stop_frac", "-"), res.get("n", 0), reason)
    elif a.json:
        print(json.dumps(res))
    else:
        print(
            "EXITQUAL  verdict=%s stop_share=%s tp_share=%s payoff=%s "
            "green_stops=%s(cut-winners) avg_stop_peak=%s n=%s {%s}" % (
                res["verdict"], res.get("stop_share", "-"), res.get("tp_share", "-"),
                res.get("payoff", "-"), res.get("green_stop_frac", "-"),
                res.get("avg_stop_mfe", "-"), res["n"], res["reason"],
            )
        )


if __name__ == "__main__":
    main()
