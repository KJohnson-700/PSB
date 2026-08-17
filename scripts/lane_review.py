#!/usr/bin/env python3
"""Per-LANE (strategy|window|side) review — the daily loop from the Codex plan.

Judges lanes by PROFIT FACTOR and avg-win/avg-loss (payoff), NOT win-rate, because a
90%-WR lane can still bleed if its losers are large. Builds the allowlist candidates
from real closed-trade evidence on the CURRENT config, per lane AND side AND band.

Usage:
  python scripts/lane_review.py                 # post-anchor era when available
  python scripts/lane_review.py --era recent    # last N sessions
  python scripts/lane_review.py --sessions 3    # last N sessions
  python scripts/lane_review.py --since 20260811_2059   # only sessions at/after this id
"""
import json, glob, os, argparse, sys
from collections import defaultdict

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ANCHOR = os.path.join(_REPO, "data/calibration/lane_cut_watchlist_anchor.json")
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    from lane_cut_watchlist import WATCH
except Exception:
    WATCH = {}

def load_exits(dirs):
    out=[]
    for d in dirs:
        try:
            for l in open(os.path.join(d,"entries.jsonl"),errors="ignore"):
                r=json.loads(l)
                if r.get("event")=="EXIT": r["_sess"]=os.path.basename(d.rstrip("/")); out.append(r)
        except FileNotFoundError: pass
    return out

def g(e,k,d=None):
    x=e.get("extra",{}); return x.get(k, e.get(k,d))

def _anchor_ts():
    try:
        with open(ANCHOR) as f:
            return str(json.load(f).get("anchor_ts") or "")
    except Exception:
        return ""

def _ts(e):
    return str(e.get("timestamp") or e.get("opened_at") or g(e, "opened_at", ""))

def _source_bucket(e):
    reason = str(e.get("reason") or "") + " " + str(g(e, "signal_reason", ""))
    resolver_path = str(g(e, "resolver_path", ""))
    side_source = str(g(e, "side_source", "")) or str(g(e, "side_src", ""))
    text = " ".join([reason, resolver_path, side_source]).lower()
    if "market_favorite" in text:
        return "market_favorite"
    if "rsi_fade" in text or "fade" in text:
        return "fade"
    return "resolver_native"

def _lane_key(e, include_source=False):
    key=f"{e.get('strategy','?')}|{g(e,'lane_window') or g(e,'window_size','?')}|{e.get('action','?')}"
    if include_source:
        key += f"|{_source_bucket(e)}"
    return key

def _entry_price(e):
    try:
        return float(e.get("entry_price") or 0.0)
    except Exception:
        return 0.0

def stats(rows):
    pnls=[float(e.get("pnl",0.0) or 0.0) for e in rows]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    gross_w=sum(wins); gross_l=abs(sum(losses))
    pf=(gross_w/gross_l) if gross_l else (float('inf') if wins else 0)
    aw=(gross_w/len(wins)) if wins else 0
    al=(gross_l/len(losses)) if losses else 0
    wl=(aw/al) if al else (float('inf') if aw else 0)
    mean_entry=sum(_entry_price(e) for e in rows)/len(rows) if rows else 0
    wr=(len(wins)/len(pnls)) if pnls else 0
    return dict(n=len(pnls), net=sum(pnls), wr=(len(wins)/len(pnls)) if pnls else 0,
                pf=pf, aw=aw, al=al, wl=wl, worst=min(pnls) if pnls else 0,
                entry=mean_entry, beat=wr-mean_entry)

def _flag(key, s):
    if s["n"] < 4:
        return ""
    lane = "|".join(key.split("|")[:3])
    source = key.split("|")[3] if key.count("|") >= 3 else "resolver_native"
    cut_n = int((WATCH.get(lane) or {}).get("cut_n") or 20)
    if s["net"] > 0 and s["wl"]>=1.3 and (s["pf"]==float('inf') or s["pf"]>1.15):
        return " ✅ALLOW"
    if s["net"] >= -15:
        return ""
    if source == "market_favorite":
        return " ⚠POLICY_BLEED"
    if s["n"] < cut_n:
        return f" 🟡ACCRUE({s['n']}/{cut_n})"
    if s["beat"] < 0:
        return " ⛔REVIEW_CUT"
    return " ⚠SIZING_BLEED"

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--sessions",type=int,default=6)
    ap.add_argument("--since",default=None)
    ap.add_argument("--era",choices=("post-anchor","recent","all"),default="post-anchor",
                    help="post-anchor avoids grading current code on known bug-era trades")
    ap.add_argument("--collapse-source",action="store_true",
                    help="hide resolver_native/market_favorite/fade split")
    a=ap.parse_args()
    dirs=sorted(glob.glob("data/paper_trades/*/"),key=os.path.getmtime,reverse=True)
    if a.since:
        dirs=[d for d in dirs if os.path.basename(d.rstrip("/")).replace("test_","")>=a.since]
    elif a.era == "all":
        pass
    else:
        dirs=dirs[:a.sessions]
    ex=load_exits(dirs)
    anchor = _anchor_ts()
    if a.era == "post-anchor" and anchor:
        ex=[e for e in ex if _ts(e) >= anchor]
    print(f"sessions scanned: {len(dirs)}  era: {a.era}  anchor: {anchor or 'none'}")
    print(f"closed trades: {len(ex)}  net ${sum(e.get('pnl',0) for e in ex):.2f}")
    if a.era != "post-anchor":
        print("warning: this view can include bug/stale-code eras; do not use it alone for lane cuts.")

    lanes=defaultdict(list)
    for e in ex:
        lanes[_lane_key(e, include_source=not a.collapse_source)].append(e)
    rows=[(k,stats(v)) for k,v in lanes.items()]
    rows.sort(key=lambda r: r[1]['net'], reverse=True)

    print(f"\n{'lane (strategy|win|side|source)':48} {'net$':>8} {'n':>3} {'WR':>4} {'entry':>5} {'BEAT':>6} {'PF':>5} {'W/L':>4} {'worst':>7}")
    print("-"*112)
    for k,s in rows:
        pf="inf" if s['pf']==float('inf') else f"{s['pf']:.2f}"
        wl="inf" if s['wl']==float('inf') else f"{s['wl']:.2f}"
        flag=_flag(k, s)
        print(f"{k:48} {s['net']:8.2f} {s['n']:3d} {s['wr']*100:3.0f}% {s['entry']*100:5.1f} {s['beat']*100:6.1f} {pf:>5} {wl:>4} {s['worst']:7.2f}{flag}")
    print("\nlegend: POLICY_BLEED = market-favorite side policy, not proof the native resolver lane is bad.")

    # entry_family split
    fam=defaultdict(list)
    for e in ex: fam[g(e,'entry_family','?')].append(e)
    print(f"\n{'entry_family':28} {'net$':>8} {'n':>3} {'PF':>5} {'W/L':>4}")
    for k,v in sorted(fam.items(),key=lambda x:-sum(float(e.get("pnl",0.0) or 0.0) for e in x[1])):
        s=stats(v); pf="inf" if s['pf']==float('inf') else f"{s['pf']:.2f}"; wl="inf" if s['wl']==float('inf') else f"{s['wl']:.2f}"
        print(f"  {str(k):26} {s['net']:8.2f} {s['n']:3d} {pf:>5} {wl:>4}")

if __name__=="__main__": main()
