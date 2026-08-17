import json, collections, math

# 1) market_id -> real outcome (YES/NO)
out={}
for l in open('data/calibration/rejected_candidates_settled.jsonl', errors='ignore'):
    if '"outcome"' not in l: continue
    try: r=json.loads(l)
    except Exception: continue
    mid=r.get('market_id'); oc=r.get('outcome')
    if mid is not None and oc in ('YES','NO') and mid not in out:
        out[str(mid)]=1 if oc=='YES' else 0
print('settled markets with an outcome: %d'%len(out))

# 2) shadow rows, DEDUPED to FIRST observation per market_id
first={}
tot=0
for l in open('data/calibration/cex_pm_lag_shadow.jsonl', errors='ignore'):
    tot+=1
    try: r=json.loads(l)
    except Exception: continue
    mid=str(r.get('market_id') or '')
    if mid and mid not in first: first[mid]=r
print('shadow rows %d -> unique markets %d  (%.1fx duplication)'%(tot,len(first),tot/max(1,len(first))))

joined=[(m,r,out[m]) for m,r in first.items() if m in out]
print('JOINED (unique markets with outcome): %d'%len(joined))
print()

def ci(k,n):
    if n==0: return 0.0,0.0
    p=k/n; se=math.sqrt(max(p*(1-p),1e-12)/n)
    return p*100, 1.96*se*100

def score(name, sel, pred):
    """pred(r) -> 1 predict UP / 0 predict DOWN / None abstain"""
    k=n=0
    for m,r,o in sel:
        d=pred(r)
        if d is None: continue
        n+=1; k+= 1 if d==o else 0
    if n==0: print('  %-42s n=0'%name); return None
    acc,h=ci(k,n)
    print('  %-42s n=%6d  acc %5.2f%% +/-%4.2f'%(name,n,acc,h))
    return (k,n,acc,h)

print('=== DIRECTIONAL ACCURACY (all joined markets) ===')
score('binance_dir (the signal)', joined, lambda r: 1 if r.get('binance_dir')=='UP' else (0 if r.get('binance_dir')=='DOWN' else None))
score('pm_implied_up (the QUOTE = benchmark)', joined, lambda r: 1 if r.get('pm_implied_up') else 0)
score('coinflip (always UP)', joined, lambda r: 1)

print()
print('=== THE TRADEABLE SUBSET: Binance DISAGREES with the mid ===')
dis=[x for x in joined if x[1].get('binance_vs_mid_disagree')]
print('  n = %d (%.1f%% of joined)'%(len(dis),100*len(dis)/max(1,len(joined))))
score('  binance_dir on disagreements', dis, lambda r: 1 if r.get('binance_dir')=='UP' else (0 if r.get('binance_dir')=='DOWN' else None))
score('  the QUOTE on those same markets', dis, lambda r: 1 if r.get('pm_implied_up') else 0)

print()
print('=== mid_lags_binance == True (the actual lag claim) ===')
lag=[x for x in joined if x[1].get('mid_lags_binance')]
print('  n = %d'%len(lag))
score('  binance_dir when mid LAGS', lag, lambda r: 1 if r.get('binance_dir')=='UP' else (0 if r.get('binance_dir')=='DOWN' else None))
score('  the QUOTE when mid LAGS', lag, lambda r: 1 if r.get('pm_implied_up') else 0)

print()
print('=== EV PER $1 STAKED, trading binance_dir, at the GRADUATION BAR ===')
print('  (buy the side binance_dir implies at pm_mid + cost; win pays 1, lose pays 0; fee on stake)')
for label, sel in [('ALL joined', joined), ('DISAGREE subset', dis), ('mid_lags subset', lag)]:
    for cost in (0.00, 0.01, 0.02):
        for fee in (0.0, 0.07):
            tot_ev=0.0; n=0
            for m,r,o in sel:
                d = 1 if r.get('binance_dir')=='UP' else (0 if r.get('binance_dir')=='DOWN' else None)
                if d is None: continue
                mid=r.get('pm_mid')
                try: mid=float(mid)
                except Exception: continue
                if not (0.0<mid<1.0): continue
                price = mid if d==1 else (1.0-mid)     # price of the side we buy
                price = min(0.99, price+cost)          # cross the spread
                won = (d==o)
                pnl = (1.0-price) if won else (-price)
                pnl -= fee*price                        # taker fee on stake
                tot_ev += pnl/price                      # per $1 staked
                n+=1
            if n: print('    %-16s cost %.2f fee %.2f -> EV/$1 %+0.4f   (n=%d)'%(label,cost,fee,tot_ev/n,n))
