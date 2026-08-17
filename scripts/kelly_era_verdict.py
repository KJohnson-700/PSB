import json, collections
rows=[]
for l in open('data/calibration/trades.jsonl'):
    try: r=json.loads(l)
    except Exception: continue
    if r.get('pnl') is None: continue
    rows.append(r)
def ts(r): return str(r.get('opened_at') or r.get('ts') or '')
def f(r,k):
    try: return float(r.get(k) or 0)
    except Exception: return 0.0
def lane(r): return '%s|%s|%s'%(r.get('strategy'),r.get('window'),r.get('action'))

def kelly(sel):
    """realized (p,b) Kelly fraction for a set of trades."""
    n=len(sel)
    W=[f(r,'pnl') for r in sel if f(r,'pnl')>0]
    L=[abs(f(r,'pnl')) for r in sel if f(r,'pnl')<=0]
    if n<4 or not W or not L: return None
    p=len(W)/n; b=(sum(W)/len(W))/(sum(L)/len(L))
    if b<=0: return None
    fstar=(b*p-(1-p))/b
    return dict(n=n,p=p,b=b,fstar=fstar,net=sum(f(r,'pnl') for r in sel))

JJ=[r for r in rows if '2026-06'<=ts(r)[:7]<='2026-07']
AU=[r for r in rows if ts(r)[:7]=='2026-08']
gj=collections.defaultdict(list); ga=collections.defaultdict(list)
for r in JJ: gj[lane(r)].append(r)
for r in AU: ga[lane(r)].append(r)
print('ERA ROW COUNTS   Jun/Jul=%d   Aug=%d   -> blend %.0f%%/%.0f%%'%(len(JJ),len(AU),100*len(JJ)/(len(JJ)+len(AU)),100*len(AU)/(len(JJ)+len(AU))))
print()
print('%-26s | %-22s | %-22s'%('lane|side','JUN/JUL (what Kelly sees)','AUGUST (what is true now)'))
print('%-26s | %5s %6s %6s %7s | %5s %6s %8s'%('','n','f*','b','net','n','f*','net'))
up_jj=[]
for k in sorted(set(list(gj)+list(ga))):
    a=kelly(gj.get(k,[])); c=kelly(ga.get(k,[]))
    if not a or a['n']<12: continue
    au='%5d %+6.2f %+8.2f'%(c['n'],c['fstar'],c['net']) if c else '%5s %6s %8s'%('-','-','-')
    print('%-26s | %5d %+6.2f %6.2f %+7.1f | %s'%(k,a['n'],a['fstar'],a['b'],a['net'],au))
    if a['fstar']>0: up_jj.append((k,a,c))
print()
print('=== THE TEST: lanes Jun/Jul-Kelly says UPSIZE (f*>0) — what did they do in AUGUST? ===')
tot=0.0; n=0; pos=0; neg=0
for k,a,c in up_jj:
    if not c: continue
    tot+=c['net']; n+=c['n']
    if c['net']>0: pos+=1
    else: neg+=1
print('  lanes Jun/Jul says UPSIZE with August data: %d   (%d positive / %d negative in Aug)'%(pos+neg,pos,neg))
print('  their combined AUGUST realized: n=%d  net %+.2f'%(n,tot))
