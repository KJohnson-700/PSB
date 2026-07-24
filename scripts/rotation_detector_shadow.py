#!/usr/bin/env python3
"""rotation_detector_shadow.py — OBSERVE-ONLY cross-asset carry detector.

WHY: the carry rotates between assets over WEEKS (xrp5m was the workhorse for a
month, decayed, sol5m inherited it — project_xrp_edge_decay_carry_rotated_to_sol5m).
A live sizer that shifts capital toward the current carrier is performance-CHASING;
recent perf is mostly variance. So we do NOT move dollars. This computes the rolling
per-asset realized carry and logs the kelly-multiplier it WOULD apply per asset, so we
can forward-measure help-vs-whipsaw before ever wiring it to sizing.

SHADOW GUARANTEE: this is a SEPARATE PROCESS. It reads entries.jsonl and appends one
line to rotation_shadow.jsonl. It never imports the bot, never writes _runtime_feedback
/ kelly_mult / any notional cap, never touches config. It physically cannot affect a
trade. The eventual LIVE version ports this same logic into the in-process settle path
and writes _runtime_feedback[asset].kelly_mult (get_drift_kelly_mult, kelly_sizer.py:257).

DISCIPLINE (baked in):
- realized = EXIT-sum from entries.jsonl (never snapshot bankroll/realized_pnl).
- good-config only (sessions >= GOOD_CONFIG_SINCE, >= MIN_SESSION_TRADES) — no
  broken-config contamination (the trap that faked calibration_correction's +$51).
- multi-window (3/7/14d) logged in PARALLEL — window-length is the ONE param we
  forward-tune; the forward-settle later picks the winner.
- ASYMMETRIC fade-before-raise: FADE (mult<1) trips on modest evidence; RAISE (mult>1)
  needs larger n + K consecutive confirmations. Fade side can qualify for live first.
- WINNER_PROTECTED assets can never receive a FADE.
- slew-rate clamp so even the eventual live version can't whipsaw.
"""
import json, os, sys
import datetime as dt
from collections import defaultdict
from statistics import median

# ---- config (all shadow; forward-tunable) ------------------------------------
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESS_ROOT = os.path.join(REPO, "data", "paper_trades")
OUT = os.path.join(REPO, "data", "calibration", "rotation_shadow.jsonl")

WINDOWS_DAYS = [3, 7, 14]
GOOD_CONFIG_SINCE = "2026-07-21"   # sessions before this date excluded (decayed-config era)
MIN_SESSION_TRADES = 20            # drop aborted / tiny sessions
BASELINE_MIN_N = 5                 # asset needs >=this many trades to vote in the field median
MULT_BAND = (0.6, 1.4)
SLEW_MAX = 0.10                    # max |mult - prev_mult| per eval
FADE_THRESHOLD = 2.0              # $/trade BELOW field to consider fading
RAISE_THRESHOLD = 3.0            # $/trade ABOVE field to consider raising (stricter)
FADE_SLOPE = 0.04                # mult delta per $/trade of rel (fade side)
RAISE_SLOPE = 0.03              # mult delta per $/trade of rel (raise side, gentler)
FADE_MIN_N = 12
RAISE_MIN_N = 20
RAISE_CONFIRM_EVALS = 3          # consecutive raise evals before a raise mult is emitted
WINNER_PROTECTED = {"sol"}        # never emit FADE for these


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def asset_of(strategy):
    return strategy.replace("_macro", "")


def parse_ts(s):
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def load_exits(now):
    """Return list of (asset, pnl, exit_ts) across good-config sessions."""
    since = dt.datetime.strptime(GOOD_CONFIG_SINCE, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    out = []
    included, skipped = [], []
    for name in sorted(os.listdir(SESS_ROOT)):
        if not name.startswith("test_"):
            continue
        # session date from the test_YYYYMMDD_HHMMSS name
        try:
            sdate = dt.datetime.strptime(name.split("_")[1], "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        except (IndexError, ValueError):
            continue
        if sdate < since:
            continue
        path = os.path.join(SESS_ROOT, name, "entries.jsonl")
        if not os.path.isfile(path):
            continue
        exits = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if r.get("event") != "EXIT":
                        continue
                    ts_s = r.get("timestamp")
                    if not ts_s:
                        continue
                    exits.append((asset_of(r.get("strategy", "")), float(r.get("pnl", 0) or 0), parse_ts(ts_s)))
        except (OSError, ValueError):
            continue
        if len(exits) < MIN_SESSION_TRADES:
            skipped.append((name, len(exits)))
            continue
        included.append((name, len(exits)))
        out.extend(exits)
    return out, included, skipped


def last_state():
    """Read prior rotation_shadow rows -> {(asset,window): (prev_mult, prev_signal, confirm_count)}."""
    state = {}
    if not os.path.isfile(OUT):
        return state
    try:
        with open(OUT) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                key = (r.get("asset"), r.get("window_days"))
                state[key] = (
                    float(r.get("would_kelly_mult", 1.0) or 1.0),
                    r.get("signal", "hold"),
                    int(r.get("confirm_count", 0) or 0),
                )
    except (OSError, ValueError):
        pass
    return state


def evaluate(exits, now):
    prev = last_state()
    rows = []
    for w in WINDOWS_DAYS:
        cutoff = now - dt.timedelta(days=w)
        by_asset = defaultdict(list)
        for asset, pnl, ts in exits:
            if ts >= cutoff:
                by_asset[asset].append(pnl)
        # field = median net/trade over assets with enough samples
        npt = {a: sum(v) / len(v) for a, v in by_asset.items()}
        field_pool = [npt[a] for a, v in by_asset.items() if len(v) >= BASELINE_MIN_N]
        baseline = median(field_pool) if field_pool else 0.0
        for asset, pnls in sorted(by_asset.items()):
            n = len(pnls)
            net_per_trade = sum(pnls) / n
            wr = sum(1 for p in pnls if p > 0) / n
            rel = net_per_trade - baseline
            pmult, psig, pconfirm = prev.get((asset, w), (1.0, "hold", 0))

            signal, confirm = "hold", 0
            raw_mult = 1.0
            if rel < -FADE_THRESHOLD and n >= FADE_MIN_N and asset not in WINNER_PROTECTED:
                signal = "fade"
                raw_mult = clamp(1.0 + FADE_SLOPE * rel, *MULT_BAND)  # rel<0 -> mult<1
            elif rel > RAISE_THRESHOLD and n >= RAISE_MIN_N:
                # count consecutive raise-eligible evals; raise_pending rows also
                # continue the streak (else the counter resets every eval and the
                # confirm threshold is never reached — Codex-caught).
                confirm = pconfirm + 1 if psig in ("raise", "raise_pending") else 1
                if confirm >= RAISE_CONFIRM_EVALS:
                    signal = "raise"
                    raw_mult = clamp(1.0 + RAISE_SLOPE * rel, *MULT_BAND)
                else:
                    signal = "raise_pending"  # accumulating confirmations, mult stays 1.0
            # slew-rate clamp vs last logged mult
            would_mult = clamp(raw_mult, pmult - SLEW_MAX, pmult + SLEW_MAX)
            would_mult = clamp(would_mult, *MULT_BAND)

            rows.append({
                "ts_utc": now.isoformat(),
                "window_days": w,
                "asset": asset,
                "n": n,
                "net_per_trade": round(net_per_trade, 4),
                "wr": round(wr, 4),
                "baseline_net_per_trade": round(baseline, 4),
                "rel": round(rel, 4),
                "would_kelly_mult": round(would_mult, 4),
                "signal": signal,
                "confirm_count": confirm,
                "winner_protected": asset in WINNER_PROTECTED,
                "current_actual_kelly_mult": 1.0,  # inert: performance_feedback.enabled is false
                "mode": "shadow",
            })
    return rows


def main():
    now = dt.datetime.now(dt.timezone.utc)
    exits, included, skipped = load_exits(now)
    if not exits:
        sys.stderr.write("rotation_shadow: no good-config exits found; nothing logged\n")
        return 0
    rows = evaluate(exits, now)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # human summary to stdout (launchd log)
    print(f"rotation_shadow {now.isoformat()} | sessions in={len(included)} skip={len(skipped)} | exits={len(exits)}")
    for w in WINDOWS_DAYS:
        wr_rows = [r for r in rows if r["window_days"] == w and r["signal"] in ("fade", "raise")]
        acts = ", ".join(f"{r['asset']}:{r['signal']}->{r['would_kelly_mult']}" for r in wr_rows) or "all hold"
        print(f"  {w}d: {acts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
