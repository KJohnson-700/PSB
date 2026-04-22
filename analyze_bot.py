import re, json
from collections import defaultdict

log_files = [
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260316.log',
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260317.log',
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260319.log',
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260320.log',
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260321.log',
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260322.log',
    'C:/Users/AbuBa/Downloads/polymarket-bot/src/logs/polybot_20260323.log',
]

all_lines = []
for lf in log_files:
    try:
        with open(lf, 'r', errors='replace') as f:
            all_lines.extend(f.readlines())
    except Exception as e:
        print(f'Error: {lf}: {e}')

# Load trade outcomes
entries = []
with open('C:/Users/AbuBa/Downloads/polymarket-bot/data/paper_trades/20260320_190255/entries.jsonl') as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            entries.append(json.loads(line))
        except: pass

entry_map = {e['trade_id']: e for e in entries if e.get('event') == 'ENTRY' and 'trade_id' in e}
exit_map = {e['trade_id']: e for e in entries if e.get('event') == 'EXIT' and 'trade_id' in e}

# === TIMELINE PARSING ===
timeline = []
for i, line in enumerate(all_lines):
    s = line.strip()
    ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+', s)
    if not ts_match:
        continue
    ts_str = ts_match.group(1)

    if 'HTF BIAS:' in s:
        d = {'type': 'btc_indicator', 'ts': ts_str}
        for pattern, key in [
            (r'HTF BIAS: (\w+)', 'htf'),
            (r'Sabre=(\w+)', 'sabre'),
            (r'tension=([+-][\d.]+)', 'tension'),
            (r'4H MACD hist=([+-][\d.]+)', 'macd_4h_hist'),
            (r'(above|below)0', 'macd_4h_zero'),
            (r'15m MACD hist=([+-][\d.]+)', 'macd_15m_hist'),
            (r'RSI=([\d.]+)', 'rsi'),
            (r'Mom 15m=(\w+)', 'mom_15m'),
        ]:
            m = re.search(pattern, s)
            if m: d[key] = m.group(1)
        for state in ['BULLISH_CROSS', 'BEARISH_CROSS']:
            if state in s:
                d['macd_15m_cross'] = state
                break
        if 'macd_15m_cross' not in d: d['macd_15m_cross'] = 'NONE'
        for fld in ['tension', 'macd_4h_hist', 'macd_15m_hist', 'rsi']:
            if fld in d:
                try: d[fld] = float(d[fld])
                except: pass
        timeline.append(d)

    elif 'BTC SIGNAL:' in s:
        d = {'type': 'btc_signal', 'ts': ts_str}
        m = re.search(r'BTC SIGNAL: (\w+)', s)
        if m: d['action'] = m.group(1)
        m = re.search(r"'([^']+)'", s)
        if m: d['market_q_short'] = m.group(1)[:60]
        m = re.search(r'edge=([\d.]+)', s)
        if m: d['edge'] = float(m.group(1))
        m = re.search(r'HTF=(\w+)', s)
        if m: d['htf'] = m.group(1)
        m = re.search(r'exp=(\w+)', s)
        if m: d['exp'] = m.group(1)
        timeline.append(d)

    elif 'SOL $' in s and 'MACRO:' in s:
        d = {'type': 'sol_indicator', 'ts': ts_str}
        for pattern, key in [
            (r'MACRO: (\w+)', 'macro'),
            (r'1H=(\w+)', 'h1_trend'),
            (r'lag_opp=(\w+)', 'lag_opp'),
            (r'lag_dir=(\w+)', 'lag_dir'),
            (r'spike=(\w+)', 'btc_spike'),
            (r'RSI=([\d.]+)', 'rsi'),
            (r'corr=([\d.]+)', 'corr'),
            (r'lag_mag=([+-][\d.]+)%', 'lag_mag'),
            (r'15m MACD hist=([+-][\d.]+)', 'macd_15m_hist'),
        ]:
            m = re.search(pattern, s)
            if m: d[key] = m.group(1)
        for fld in ['rsi', 'corr', 'lag_mag', 'macd_15m_hist']:
            if fld in d:
                try: d[fld] = float(d[fld])
                except: pass
        for state in ['BULLISH_CROSS', 'BEARISH_CROSS']:
            if state in s:
                d['macd_15m_cross'] = state
                break
        if 'macd_15m_cross' not in d: d['macd_15m_cross'] = 'NONE'
        timeline.append(d)

    elif 'SOL SIGNAL:' in s:
        d = {'type': 'sol_signal', 'ts': ts_str}
        m = re.search(r'SIGNAL: (\w+)', s)
        if m: d['action'] = m.group(1)
        m = re.search(r"'([^']+)'", s)
        if m: d['market_q_short'] = m.group(1)[:60]
        m = re.search(r'edge=([\d.]+)', s)
        if m: d['edge'] = float(m.group(1))
        m = re.search(r'MACRO=(\w+)', s)
        if m: d['macro'] = m.group(1)
        m = re.search(r'exp=(\w+)', s)
        if m: d['exp'] = m.group(1)
        timeline.append(d)

# Match BTC signals to indicator context
btc_ctx = []
last_btc_ind = None
for item in timeline:
    if item['type'] == 'btc_indicator':
        last_btc_ind = item
    elif item['type'] == 'btc_signal' and last_btc_ind:
        combined = {**last_btc_ind, **item}
        btc_ctx.append(combined)

# Match SOL signals to indicator context
sol_ctx = []
last_sol_ind = None
for item in timeline:
    if item['type'] == 'sol_indicator':
        last_sol_ind = item
    elif item['type'] == 'sol_signal' and last_sol_ind:
        combined = {**last_sol_ind, **item}
        sol_ctx.append(combined)

print(f'BTC signals with context: {len(btc_ctx)}')
print(f'SOL signals with context: {len(sol_ctx)}')

# === TRADE OUTCOMES ===
btc_trades = []
for tid, e in entry_map.items():
    if e.get('strategy') != 'bitcoin' or tid not in exit_map: continue
    ex = exit_map[tid]
    btc_trades.append({
        'tid': tid, 'action': e.get('action',''),
        'pnl': ex.get('pnl',0), 'edge': e.get('edge',0),
        'entry_price': e.get('entry_price',0),
        'market_q': e.get('market_question',''),
        'ts': e.get('timestamp',''),
        'win': ex.get('pnl',0) > 0,
    })

sol_trades = []
for tid, e in entry_map.items():
    if e.get('strategy') != 'sol_lag' or tid not in exit_map: continue
    ex = exit_map[tid]
    sol_trades.append({
        'tid': tid, 'action': e.get('action',''),
        'pnl': ex.get('pnl',0), 'edge': e.get('edge',0),
        'entry_price': e.get('entry_price',0),
        'market_q': e.get('market_question',''),
        'ts': e.get('timestamp',''),
        'win': ex.get('pnl',0) > 0,
    })

def stat(group):
    n = len(group)
    wins = sum(1 for t in group if t.get('win', t.get('pnl',0) > 0))
    pnl = sum(t.get('pnl',0) for t in group)
    wr = wins/n*100 if n else 0
    return f'n={n}, WR={wr:.1f}%, PnL=${pnl:.2f}'

# ===== REPORT =====
print()
print('='*70)
print('BTC STRATEGY: INDICATOR DISTRIBUTION AT SIGNAL EVENTS (n=495 signals)')
print('='*70)

# HTF bias
print('\nHTF Bias at signal time:')
htf_grp = defaultdict(int)
for d in btc_ctx: htf_grp[d.get('htf','?')] += 1
for k in sorted(htf_grp): print(f'  {k}: {htf_grp[k]} ({htf_grp[k]/len(btc_ctx)*100:.1f}%)')

# Sabre vs HTF conflict
print('\nSabre/HTF alignment:')
sa = [d for d in btc_ctx if d.get('sabre')=='BULL' and d.get('htf')=='BULLISH']
sc = [d for d in btc_ctx if d.get('sabre')=='BULL' and d.get('htf')=='BEARISH']
se = [d for d in btc_ctx if d.get('sabre')=='BEAR' and d.get('htf')=='BEARISH']
sf = [d for d in btc_ctx if d.get('sabre')=='BEAR' and d.get('htf')=='BULLISH']
print(f'  Sabre BULL + HTF BULLISH (aligned): n={len(sa)}')
print(f'  Sabre BEAR + HTF BEARISH (aligned): n={len(se)}')
print(f'  Sabre BULL + HTF BEARISH (CONFLICT): n={len(sc)} ** KEY ISSUE **')
print(f'  Sabre BEAR + HTF BULLISH (conflict): n={len(sf)}')

# The conflict: Sabre BULL + HTF BEARISH
print('\n  Sample conflict cases (Sabre BULL but HTF BEARISH):')
for d in sc[:5]:
    print(f'    4H hist={d.get("macd_4h_hist","?")}, 4H zero={d.get("macd_4h_zero","?")}, '
          f'RSI={d.get("rsi","?")}, tension={d.get("tension","?")}')

# 4H MACD analysis
print('\n4H MACD histogram at signal:')
h4 = [d.get('macd_4h_hist',0) for d in btc_ctx if 'macd_4h_hist' in d]
if h4:
    print(f'  Range: [{min(h4):.0f}, {max(h4):.0f}], mean={sum(h4)/len(h4):.0f}')
    # Large positive MACD + BEARISH bias = potential sign error
    lp_bear = [d for d in btc_ctx if d.get('macd_4h_hist',0) > 200 and d.get('htf') == 'BEARISH']
    print(f'  4H hist>200 + HTF=BEARISH: n={len(lp_bear)} (MACD sign vs HTF conflict)')
    for d in lp_bear[:5]:
        print(f'    hist={d.get("macd_4h_hist","?")}, sabre={d.get("sabre","?")}, '
              f'4H_zero={d.get("macd_4h_zero","?")}, RSI={d.get("rsi","?")}')

# RSI distribution
print('\nRSI at signal time:')
rsi_vals = [d.get('rsi',0) for d in btc_ctx if 'rsi' in d]
if rsi_vals:
    print(f'  Range: [{min(rsi_vals):.0f}, {max(rsi_vals):.0f}], mean={sum(rsi_vals)/len(rsi_vals):.1f}')
    for label, lo, hi in [('<40',0,40),('40-50',40,50),('50-60',50,60),('60-70',60,70),('70+',70,200)]:
        cnt = sum(1 for r in rsi_vals if lo <= r < hi)
        print(f'  RSI {label}: n={cnt} ({cnt/len(rsi_vals)*100:.1f}%)')

# Sabre tension
print('\nSabre tension at signal:')
tens = [d.get('tension',0) for d in btc_ctx if 'tension' in d]
if tens:
    print(f'  Range: [{min(tens):.1f}, {max(tens):.1f}], mean={sum(tens)/len(tens):.2f}')
    high_t = [d for d in btc_ctx if abs(d.get('tension',0)) > 2.0]
    print(f'  |tension|>2.0 (stretched, mean-reversion risk): n={len(high_t)} ({len(high_t)/len(btc_ctx)*100:.1f}%)')

# 15m MACD crossover
print('\n15m MACD crossover at signal:')
cross_grp = defaultdict(int)
for d in btc_ctx: cross_grp[d.get('macd_15m_cross','NONE')] += 1
for k, v in sorted(cross_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

# Momentum
print('\nMomentum 15m direction at signal:')
mom_grp = defaultdict(int)
for d in btc_ctx: mom_grp[d.get('mom_15m','NONE')] += 1
for k, v in sorted(mom_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

# Exposure tier
print('\nExposure tier:')
exp_grp = defaultdict(int)
for d in btc_ctx: exp_grp[d.get('exp','missing')] += 1
for k, v in sorted(exp_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

print()
print('='*70)
print('SOL STRATEGY: INDICATOR DISTRIBUTION AT SIGNAL EVENTS')
print('='*70)
print(f'Total SOL signal events: {len(sol_ctx)}')

print('\nMacro trend at signal:')
macro_grp = defaultdict(int)
for d in sol_ctx: macro_grp[d.get('macro','?')] += 1
for k, v in sorted(macro_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

print('\nLag opportunity:')
lag_grp = defaultdict(int)
for d in sol_ctx: lag_grp[d.get('lag_opp','?')] += 1
for k, v in sorted(lag_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

print('\nBTC spike detected:')
spike_grp = defaultdict(int)
for d in sol_ctx: spike_grp[d.get('btc_spike','?')] += 1
for k, v in sorted(spike_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

print('\nBTC-SOL correlation at signal:')
corr_vals = [d.get('corr',0) for d in sol_ctx if 'corr' in d]
if corr_vals:
    print(f'  Range: [{min(corr_vals):.2f}, {max(corr_vals):.2f}], mean={sum(corr_vals)/len(corr_vals):.2f}')
    for label, lo, hi in [('<0.5',0,0.5),('0.5-0.7',0.5,0.7),('0.7-0.85',0.7,0.85),('>0.85',0.85,2)]:
        cnt = sum(1 for c in corr_vals if lo <= c < hi)
        print(f'  corr {label}: n={cnt}')

print('\nSOL 15m MACD crossover:')
sc_grp = defaultdict(int)
for d in sol_ctx: sc_grp[d.get('macd_15m_cross','NONE')] += 1
for k, v in sorted(sc_grp.items(), key=lambda x:-x[1]): print(f'  {k}: {v}')

print()
print('='*70)
print('TRADE OUTCOME ANALYSIS (78 BTC + 50 SOL closed trades)')
print('='*70)

print('\nBTC overall:', stat(btc_trades))
print('SOL overall:', stat(sol_trades))

print('\nBTC by edge bucket:')
for bucket, lo, hi in [('<0.08',0,0.08),('0.08-0.10',0.08,0.10),('0.10-0.12',0.10,0.12),('0.12-0.15',0.12,0.15),('>=0.15',0.15,5)]:
    grp = [t for t in btc_trades if lo <= t['edge'] < hi]
    if grp: print(f'  Edge {bucket}: {stat(grp)}')

print('\nBTC by entry price:')
for bucket, lo, hi in [('<0.40',0,0.40),('0.40-0.50',0.40,0.50),('0.50-0.60',0.50,0.60),('>=0.60',0.60,2)]:
    grp = [t for t in btc_trades if lo <= t['entry_price'] < hi]
    if grp: print(f'  EP {bucket}: {stat(grp)}')

print('\nSOL by action:')
for action in ['BUY_YES','SELL_YES']:
    grp = [t for t in sol_trades if t['action'] == action]
    if grp: print(f'  {action}: {stat(grp)}')

print('\nSOL by edge bucket:')
for bucket, lo, hi in [('<0.06',0,0.06),('0.06-0.09',0.06,0.09),('>=0.09',0.09,5)]:
    grp = [t for t in sol_trades if lo <= t['edge'] < hi]
    if grp: print(f'  Edge {bucket}: {stat(grp)}')

print('\nSOL by entry price:')
for bucket, lo, hi in [('<0.35',0,0.35),('0.35-0.45',0.35,0.45),('0.45-0.50',0.45,0.50),('>=0.50',0.50,2)]:
    grp = [t for t in sol_trades if lo <= t['entry_price'] < hi]
    if grp: print(f'  EP {bucket}: {stat(grp)}')

# Cross-reference signal actions vs outcomes
print('\n--- ACTION DISTRIBUTION cross-check ---')
print(f'BTC signals total: {len(btc_ctx)}, breakdown:')
act_grp = defaultdict(int)
for d in btc_ctx: act_grp[d.get('action','?')] += 1
for k, v in sorted(act_grp.items(), key=lambda x:-x[1]):
    print(f'  {k}: {v} ({v/len(btc_ctx)*100:.1f}%)')

print(f'\nBTC trades (from 20260320 session) by action:')
act_trade_grp = defaultdict(list)
for t in btc_trades: act_trade_grp[t['action']].append(t)
for k, grp in act_trade_grp.items():
    print(f'  {k}: {stat(grp)}')
