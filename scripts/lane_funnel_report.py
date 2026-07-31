#!/usr/bin/env python3
"""Per-lane frequency funnel (2026-07-30, Codex paper-vs-live sweep #4).

For each lane (strategy|window|side) shows the funnel:
  candidates  -> rejected (top reasons)  -> filled  -> exited (WR, pnl)
plus fill-quality (avg fill_ratio, fail-open count) and execution drag (raw vs exec pnl).

Sources: data/calibration/rejected_candidates.jsonl (streamed, time-filtered to the
session window), data/paper_trades/<sid>/entries.jsonl (ENTRY+EXIT).

Usage: python scripts/lane_funnel_report.py <session_id> [<session_id2> ...]
       (defaults to the newest test_* session)
"""
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REJECTS = os.path.join(ROOT, "data", "calibration", "rejected_candidates.jsonl")
PT = os.path.join(ROOT, "data", "paper_trades")


def _side(a):
    a = str(a or "").upper()
    return "NO" if a in ("BUY_NO", "SHORT", "NO") else ("YES" if a in ("BUY_YES", "LONG", "YES") else a)


def _short(s):
    return str(s or "?").replace("_macro", "")


def load_session(sid):
    """Return (fills, exits, ts_lo, ts_hi) for a session's entries.jsonl."""
    f = os.path.join(PT, sid, "entries.jsonl")
    ent, exits, ts = {}, [], []
    if not os.path.exists(f):
        return ent, exits, None, None
    for l in open(f):
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("timestamp"):
            ts.append(r["timestamp"])
        if r.get("event") == "ENTRY":
            ent[r.get("trade_id")] = r
        elif r.get("event") == "EXIT":
            exits.append(r)
    return ent, exits, (min(ts) if ts else None), (max(ts) if ts else None)


def main(argv):
    sids = argv[1:]
    if not sids:
        cands = sorted([d for d in os.listdir(PT) if d.startswith("test_")])
        sids = cands[-1:] if cands else []
    if not sids:
        print("no sessions found")
        return

    for sid in sids:
        ent, exits, ts_lo, ts_hi = load_session(sid)
        # lane accumulators
        lanes = defaultdict(lambda: {
            "rej": 0, "rej_reasons": defaultdict(int), "fills": 0, "exits": 0,
            "w": 0, "pnl": 0.0, "fill_ratio": [], "failopen": 0, "raw": 0.0, "exe": 0.0,
        })

        # fills + exits from entries.jsonl
        for tid, e in ent.items():
            ex = e.get("extra", {}) or {}
            k = (_short(e.get("strategy")), str(ex.get("window_size") or "?"), _side(e.get("action") or e.get("side")))
            L = lanes[k]
            L["fills"] += 1
            pfq = ex.get("paper_fill_quality") or {}
            if pfq.get("sim_fill_ratio") is not None:
                L["fill_ratio"].append(float(pfq["sim_fill_ratio"]))
            if pfq.get("paper_fill_model") in ("signal_price_fail_open", "fail_open"):
                L["failopen"] += 1
        for x in exits:
            e = ent.get(x.get("trade_id"), {})
            ex = e.get("extra", {}) or {}
            k = (_short(x.get("strategy")), str(ex.get("window_size") or "?"), _side(x.get("action") or x.get("side")))
            L = lanes[k]
            L["exits"] += 1
            pnl = float(x.get("pnl") or 0)
            L["pnl"] += pnl
            if pnl > 0:
                L["w"] += 1
            # raw_signal_pnl / execution_adjusted_pnl live on the EXIT record (top-level
            # or its own extra / exit_telemetry), NOT the entry extra.
            xex = x.get("extra", {}) or {}
            xtel = xex.get("exit_telemetry", {}) or {}
            raw = x.get("raw_signal_pnl", xex.get("raw_signal_pnl", xtel.get("raw_signal_pnl")))
            exe = x.get("execution_adjusted_pnl", xex.get("execution_adjusted_pnl", xtel.get("execution_adjusted_pnl")))
            L["raw"] += float(raw) if raw is not None else pnl
            L["exe"] += float(exe) if exe is not None else pnl

        # rejects (streamed, time-filtered to the session window)
        rej_total = 0
        if os.path.exists(REJECTS) and ts_lo:
            for l in open(REJECTS):
                # cheap ts pre-filter without full parse
                if '"ts"' not in l:
                    continue
                try:
                    r = json.loads(l)
                except Exception:
                    continue
                rt = r.get("ts")
                if rt is None or rt < ts_lo or rt > ts_hi:
                    continue
                k = (_short(r.get("strategy")), str(r.get("window") or "?"), _side(r.get("side")))
                L = lanes[k]
                L["rej"] += 1
                L["rej_reasons"][str(r.get("reason") or "?").split(":")[0]] += 1
                rej_total += 1

        print("\n" + "=" * 118)
        print(f"LANE FUNNEL — {sid}   window [{ts_lo} .. {ts_hi}]   rejects_in_window={rej_total}")
        print("=" * 118)
        print(f"{'lane':<20}{'cand':>6}{'rej':>6}{'fill':>5}{'exit':>5}{'WR':>5}{'pnl':>9}{'drag':>8}{'fillr':>6}{'failopen':>9}  top_reject")
        print("-" * 118)
        # sort by candidates desc
        def cand(L):
            return L["rej"] + L["fills"]
        for k, L in sorted(lanes.items(), key=lambda kv: -cand(kv[1])):
            c = cand(L)
            if c == 0:
                continue
            wr = (L["w"] / L["exits"] * 100) if L["exits"] else 0
            drag = L["raw"] - L["exe"]
            fr = (sum(L["fill_ratio"]) / len(L["fill_ratio"])) if L["fill_ratio"] else None
            top = ",".join(f"{r}×{n}" for r, n in sorted(L["rej_reasons"].items(), key=lambda kv: -kv[1])[:2])
            name = f"{k[0]}|{k[1]}|{k[2]}"
            print(f"{name:<20}{c:>6}{L['rej']:>6}{L['fills']:>5}{L['exits']:>5}"
                  f"{(f'{wr:.0f}%' if L['exits'] else '—'):>5}{L['pnl']:>+9.2f}{drag:>+8.2f}"
                  f"{(f'{fr:.2f}' if fr is not None else '—'):>6}{L['failopen']:>9}  {top}")
        print("-" * 118)
        print("cand=rej+fill (candidates that reached side-selection). fill=entered. drag=raw-exec pnl (fees/slippage).")
        print("fillr=avg sim_fill_ratio. failopen=paper book-snapshot fail-open fills (fake-fill risk).")


if __name__ == "__main__":
    main(sys.argv)
