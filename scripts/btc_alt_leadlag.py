#!/usr/bin/env python3
"""BTC->alt lead-lag on Binance 1s ticks. Peak CCF lag>0 = BTC leads (tradeable); lag0 = simultaneous (dead)."""
import urllib.request, json, time, math, sys

def fetch_1s(sym, start_ms, end_ms):
    out = {}; cur = start_ms
    while cur < end_ms:
        url = f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=1s&startTime={cur}&limit=1000'
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    d = json.load(r); break
            except Exception:
                if attempt == 2: raise
                time.sleep(0.4)
        if not d: break
        for c in d: out[c[0] // 1000] = float(c[4])
        cur = d[-1][0] + 1000
        if len(d) < 1000: break
    return out

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
now = int(time.time()); start = (now - HOURS * 3600) * 1000; end = now * 1000
syms = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'DOGE': 'DOGEUSDT'}
series = {}
for name, s in syms.items():
    series[name] = fetch_1s(s, start, end)
    print(f'{name}: {len(series[name])} 1s closes', flush=True)

common = set(series['BTC'])
for n in series: common &= set(series[n])
common = sorted(common)
print('common seconds:', len(common), flush=True)

def rets(name):
    cl = series[name]; r = {}
    for i in range(1, len(common)):
        t0, t1 = common[i - 1], common[i]
        if t1 - t0 != 1: continue
        p0, p1 = cl[t0], cl[t1]
        if p0 > 0: r[t1] = math.log(p1 / p0)
    return r
R = {n: rets(n) for n in series}
btc = R['BTC']; tset = sorted(btc)

def corr(x, y):
    n = len(x)
    if n < 10: return 0.0, n
    mx = sum(x) / n; my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x); syy = sum((b - my) ** 2 for b in y)
    return (sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0), n

alts = ['ETH', 'DOGE']
print('\ncorr(BTC_ret[t], ALT_ret[t+L]); L>0 => BTC LEADS alt by L sec')
print(f"{'lag':>4}" + ''.join(f'{n:>9}' for n in alts))
peak = {a: (0, -9) for a in alts}
prof = {}
for L in range(-6, 11):
    row = f'{L:>4}'
    for alt in alts:
        a = R[alt]
        xs = []; ys = []
        for t in tset:
            u = t + L
            if u in a: xs.append(btc[t]); ys.append(a[u])
        c, n = corr(xs, ys)
        prof[(alt, L)] = c
        if c > peak[alt][1]: peak[alt] = (L, c)
        row += f'{c:>9.3f}'
    print(row, flush=True)
print()
for alt in alts:
    L, c = peak[alt]
    v = 'SIMULTANEOUS (no tradeable lead)' if L == 0 else (f'BTC LEADS by {L}s (TRADEABLE)' if L > 0 else f'BTC LAGS {alt} {-L}s')
    c0 = prof[(alt, 0)]
    print(f'{alt}: peak {c:.3f} @ lag {L:+d} | contemp(lag0)={c0:.3f} -> {v}')
