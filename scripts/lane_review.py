#!/usr/bin/env python3
"""Per-LANE (strategy|window|side) review — the daily loop from the Codex plan.

Judges lanes by PROFIT FACTOR and avg-win/avg-loss (payoff), NOT win-rate, because a
90%-WR lane can still bleed if its losers are large. Builds the allowlist candidates
from real closed-trade evidence on the CURRENT config, per lane AND side AND band.

Usage:
  python scripts/lane_review.py                 # last 6 sessions
  python scripts/lane_review.py --sessions 3    # last N sessions
  python scripts/lane_review.py --since 20260811_2059   # only sessions at/after this id
"""
import json, glob, os, argparse
from collections import defaultdict

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

def stats(pnls):
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    gross_w=sum(wins); gross_l=abs(sum(losses))
    pf=(gross_w/gross_l) if gross_l else (float('inf') if wins else 0)
    aw=(gross_w/len(wins)) if wins else 0
    al=(gross_l/len(losses)) if losses else 0
    wl=(aw/al) if al else (float('inf') if aw else 0)
    return dict(n=len(pnls), net=sum(pnls), wr=(len(wins)/len(pnls)) if pnls else 0,
                pf=pf, aw=aw, al=al, wl=wl, worst=min(pnls) if pnls else 0)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--sessions",type=int,default=6)
    ap.add_argument("--since",default=None); a=ap.parse_args()
    dirs=sorted(glob.glob("data/paper_trades/*/"),key=os.path.getmtime,reverse=True)
    if a.since:
        dirs=[d for d in dirs if os.path.basename(d.rstrip("/")).replace("test_","")>=a.since]
    else:
        dirs=dirs[:a.sessions]
    ex=load_exits(dirs)
    print(f"sessions: {len(dirs)}  closed trades: {len(ex)}  net ${sum(e.get('pnl',0) for e in ex):.2f}")

    lanes=defaultdict(list)
    for e in ex:
        key=f"{e.get('strategy','?'):11}|{g(e,'lane_window') or g(e,'window_size','?'):3}|{e.get('action','?')}"
        lanes[key].append(e.get("pnl",0.0))
    rows=[(k,stats(v)) for k,v in lanes.items()]
    def pf_key(r):
        pf=r[1]['pf']; return -(99 if pf==float('inf') else pf)
    rows.sort(key=lambda r: r[1]['net'], reverse=True)

    print(f"\n{'lane (strategy|win|side)':30} {'net$':>8} {'n':>3} {'WR':>4} {'PF':>5} {'avgW':>6} {'avgL':>7} {'W/L':>4} {'worst':>7}")
    print("-"*92)
    for k,s in rows:
        pf="inf" if s['pf']==float('inf') else f"{s['pf']:.2f}"
        wl="inf" if s['wl']==float('inf') else f"{s['wl']:.2f}"
        flag=""
        if s['n']>=4:
            if s['net']>0 and s['wl']>=1.3 and (s['pf']==float('inf') or s['pf']>1.15): flag=" ✅ALLOW"
            elif s['net']<-15: flag=" ⛔BLEED"
        print(f"{k:30} {s['net']:8.2f} {s['n']:3d} {s['wr']*100:3.0f}% {pf:>5} {s['aw']:6.2f} {s['al']:7.2f} {wl:>4} {s['worst']:7.2f}{flag}")

    # entry_family split
    fam=defaultdict(list)
    for e in ex: fam[g(e,'entry_family','?')].append(e.get('pnl',0.0))
    print(f"\n{'entry_family':28} {'net$':>8} {'n':>3} {'PF':>5} {'W/L':>4}")
    for k,v in sorted(fam.items(),key=lambda x:-sum(x[1])):
        s=stats(v); pf="inf" if s['pf']==float('inf') else f"{s['pf']:.2f}"; wl="inf" if s['wl']==float('inf') else f"{s['wl']:.2f}"
        print(f"  {str(k):26} {s['net']:8.2f} {s['n']:3d} {pf:>5} {wl:>4}")

if __name__=="__main__": main()
