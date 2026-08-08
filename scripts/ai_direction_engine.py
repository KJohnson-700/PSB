#!/usr/bin/env python3
"""AI-DIRECTION ENGINE — OBSERVE-ONLY, model-swappable (Moon Dev ModelFactory seam).

Operator ask (2026-08-03): test an AI-driven direction layer, model-agnostic (MiniMax / Claude /
future models swap behind one seam), benchmarked head-to-head vs the deterministic tape_map champion
(baseline 53%@15m / 60%@60m) — forward, in paper, logged-before-acting. Drives NO trades.

Every `interval` s, for each asset it reads the latest tape_map snapshot (raw features), then asks
EACH configured provider (via src/analysis/model_factory.ModelFactory) for a NEXT-15-MIN direction
call, and logs every provider's decision + the tape_map champion at the same instant to
data/calibration/ai_direction_shadow.jsonl. scripts/ai_direction_score.py scores each provider vs
tape_map on identical snapshots.

Cost: (#providers x #assets) calls / interval on each model's quota. Default providers=minimax.
Add claude with --providers minimax,claude (note: uses your Claude quota). Usage:
  nohup .venv/bin/python scripts/ai_direction_engine.py --providers minimax,claude --interval 300 \
      >> data/calibration/ai_direction_engine.log 2>&1 &
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.analysis.model_factory import ModelFactory, TapeMapProvider  # noqa: E402

MAP = ROOT / "data/calibration/tape_map.jsonl"
OUT = ROOT / "data/calibration/ai_direction_shadow.jsonl"
ASSETS = ["bitcoin", "sol_macro", "eth_macro", "hype_macro", "xrp_macro", "doge_macro", "bnb_macro"]
# Phase 1 (2026-08-07): log EVERY horizon, not just 15m. 15m was coinflip for all signals
# (qwen 52% best); the theory (n6058) is edge lives at 1h. The scorer already forward-joins by
# horizon_min, so logging 5/15/60 gives per-horizon accuracy for free -> find WHERE a signal beats
# coinflip, then wire THAT into the direction-override seam. Configurable via --horizons.
DEFAULT_HORIZONS = [5, 15, 60]
_CHAMPION = TapeMapProvider()


def _latest_features():
    latest = {}
    try:
        with open(MAP) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("asset") in ASSETS:
                    latest[d["asset"]] = d
    except FileNotFoundError:
        pass
    return latest


def _tick(providers, now_fn, horizons):
    latest = _latest_features()
    n = 0
    for asset in ASSETS:
        feats = latest.get(asset)
        if not feats:
            continue
        label = asset.replace("_macro", "").upper()
        # The tape_map champion reads the CURRENT tape once — horizon-agnostic; same dir logged at
        # every horizon so the scorer joins it to each future price. Models ARE horizon-specific, so
        # ask each provider per horizon.
        champ = _CHAMPION.predict_direction(label, feats, horizons[0]) or {}
        ts = now_fn()
        for h in horizons:
            decisions = {}
            for p in providers:
                t0 = time.time()
                obj = p.predict_direction(label, feats, h)
                decisions[p.name] = {
                    "dir": (obj or {}).get("dir"),
                    "conf": (obj or {}).get("conf"),
                    "why": (obj or {}).get("why"),
                    "ms": round((time.time() - t0) * 1000),
                    "error": None if obj else "no_decision",
                }
            rec = {
                "ts": ts,
                "asset": asset,
                "horizon_min": h,
                "price": feats.get("price"),
                "decisions": decisions,                       # {provider: {dir,conf,why,ms,error}}
                "tape_dir": champ.get("dir"),                 # deterministic champion, same instant
                "tape_conf": champ.get("conf"),
                "features": {k: feats.get(k) for k in
                             ("rsi_14", "macd_signs", "ema_dir", "vol_pct", "trend_dir_label")},
            }
            OUT.parent.mkdir(parents=True, exist_ok=True)
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
            tags = "  ".join(
                f"{name}={d['dir']}(c{d['conf']})" if d["dir"] else f"{name}=ERR"
                for name, d in decisions.items())
            print(f"[{asset:11s} h={h:>2}m] {tags}   tape={rec['tape_dir']}({rec['tape_conf']})", flush=True)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="minimax",
                    help="comma list of ModelFactory providers, e.g. minimax,claude")
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS),
                    help="comma list of prediction horizons in minutes, e.g. 5,15,60")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    specs = [s.strip() for s in args.providers.split(",") if s.strip()]
    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
    providers = ModelFactory.create_all(specs)
    print(f"AI-direction engine: providers={[p.name for p in providers]} "
          f"(available={ModelFactory.available()}) interval={args.interval}s horizons={horizons}m",
          flush=True)
    while True:
        try:
            n = _tick(providers, lambda: time.time(), horizons)
            print(f"  tick logged {n} rows at {time.strftime('%H:%M:%S')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  tick error: {e}", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
