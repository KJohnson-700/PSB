import json,re,subprocess,glob,collections
# 1) market_id -> resolution outcome (YES/NO) from all bot logs
res={}
rx=re.compile(r"Market (\d+) resolved: (YES|NO)")
for lg in glob.glob("data/logs/local_bot_restart_*.log")+glob.glob("data/logs/polybot_2026072*.log"):
    try:
        for l in open(lg,errors='ignore'):
            m=rx.search(l)
            if m: res[m.group(1)]=m.group(2)
    except FileNotFoundError: pass
print(f"resolved markets known: {len(res)}")

# 2) recent blocked BTC shorts from reject log (tail) — the gate-blocked candidates
out=subprocess.run(["tail","-200000","data/calibration/rejected_candidates.jsonl"],capture_output=True,text=True).stdout
blocked=[]
for l in out.splitlines():
    l=l.strip()
    if not l: continue
    try: d=json.loads(l)
    except Exception: continue
    if d.get('strategy')!='bitcoin': continue
    rsn=str(d.get('reason') or d.get('gate_reason') or '')
    if 'buy_no' in rsn and ('disabl' in rsn or 'bearish' in rsn) or 'bull_regime' in rsn and 'short' in rsn:
        mid=str(d.get('market_id') or '')
        if mid in res:
            blocked.append((mid,rsn,res[mid],d.get('yes_price'),d.get('est_prob_up') or d.get('raw_est_prob')))

print(f"blocked BTC shorts joinable to a resolution: {len(blocked)}")
if not blocked:
    print("none settled yet — markets not resolved in captured logs"); raise SystemExit
# SHORT wins if outcome == NO (market went down)
byreason=collections.defaultdict(lambda:{'n':0,'win':0,'ret':0.0})
tot={'n':0,'win':0,'ret':0.0}
for mid,rsn,outc,yp,est in blocked:
    win = 1 if outc=="NO" else 0
    try:
        p=float(yp); noprice=1.0-p
        r = (1.0/noprice - 1.0) if win else -1.0   # per $1 staked on NO
    except: r=0.0
    for bucket in (byreason[rsn],tot):
        bucket['n']+=1; bucket['win']+=win; bucket['ret']+=r
print(f"\n{'gate reason':<34}{'n':>5}{'shortWR':>9}{'EV/$staked':>11}")
for rsn,d in sorted(byreason.items(),key=lambda x:-x[1]['n']):
    print(f"{rsn:<34}{d['n']:>5}{d['win']/d['n']:>8.0%}{d['ret']/d['n']:>+11.2f}")
print(f"{'TOTAL':<34}{tot['n']:>5}{tot['win']/tot['n']:>8.0%}{tot['ret']/tot['n']:>+11.2f}")
print(f"\nSHORT wins when market resolves NO (down). WR>50% & EV>0 => the gate is blocking WINNERS (re-admit).")
print("WR<50% & EV<0 => gate is correctly blocking losers (operator intuition wrong for this side).")
