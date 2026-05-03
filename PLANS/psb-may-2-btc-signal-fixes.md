# Plan: Fix BTC Signal Dead Zone + Scanner Latency

## Context

Paper trading session `test_20260502_051838`:
- Bankroll: $500 → $550.52 (+$50.52 realized, 50 trades)
- BTC: **0 signals all day** despite HTF = BULLISH, BTC $78,425 near resistance
- SOL/ETH/HYPE/XRP: 0 signals (expected — market conditions)
- Scanner: 45 updown markets found but 16-17 skips per cycle for `outside_entry_window`

---

## Bug 1 (Critical): BTC `ltf_strength=0.00` — HTF Bullish But Zero Entries

**Root cause confirmed in `bitcoin.py` line 418–466:**

The BTC strategy reads LTF confirmation from **15m MACD only**. Today's 15m MACD histogram = -5.57 (BEARISH) while 4H MACD histogram = +144 (strongly BULLISH). These timeframes are diverging.

The code at line 800 logs:
```
Anti-LTF gate passed: LONG — early momentum, strength=0.00 (unconfirmed)
```
This means `ltf_strength=0.00` is below the 0.50 confirmation threshold, so the anti-LTF gate does NOT block entry. **But signals are still zero.** The block is coming AFTER this check — somewhere in the market evaluation loop.

**Trace the actual skip reason:**
```
Bitcoin strategy: 0 signals (HTF=BULLISH, top_skip=outside_entry_window, ai_calls=0)
top_skips: {'outside_entry_window': 16, 'buy_yes_disabled': 2}
```

The `buy_yes_disabled: 2` suggests BUY_YES was disabled. Check `config/settings.yaml` for `disable_buy_yes`.

**Fix in `bitcoin.py`:**

1. **When HTF bias = BULLISH and 4H MACD histogram is strongly positive (>50), allow entries even when 15m LTF shows no confirmation.** The divergence between 4H and 15m means 15m is catching up, not that the trade is wrong. Add:

```python
# In _check_lower_tf_confirmation or the calling code
# If HTF is strongly bullish and 4H histogram > threshold, reduce confirmation requirement
htf_confident = htf_bias == "BULLISH" and macd_4h.histogram > 50
if htf_confident and allowed_side == "LONG":
    # 4H momentum is strong enough to justify entry without 15m confirmation
    # Still use 15m as a veto — if 15m is deeply bearish (histogram < -20), skip
    if macd_15m.histogram < -20:
        # 15m strongly against us — skip despite HTF strength
        return True, strength, reasons  # confirmed = skip
```

2. **Add `ltf_strength` to the ops_json gate distributions** so it appears in scan diagnostics (currently `n=0`).

3. **Check `disable_buy_yes` in settings.yaml** — if it's true, BTC BUY_YES is blocked and BTC appears silent. Either set to false or use this opportunity to verify BTC should only be SHORT in current regime.

---

## Bug 2: `lag_dir=NONE` Despite BTC Intraday Move

**Root cause in `sol_macro.py` around line 900–960:**

The lag/timing bonus uses `calc_correlation()` result as a **hard gate** — if correlation < ~0.20, the timing bonus becomes +0.000 regardless of BTC move magnitude.

From today's log:
```
BTC-SOL corr=0.28 lag_opp=False lag_dir=NONE lag_mag=+0.00%
Timing: bonus=+0.000 [low corr (0.28)]
```

Correlation of 0.28 should dampen the bonus, not zero it. The code is likely:
```python
if correlation < 0.20:
    timing_bonus = 0.0  # hard gate
```

**Fix in `sol_macro.py` `_check_entry_timing` or `calc_correlation`:**

Replace hard gate with dampening multiplier:
```python
# Instead of:
if correlation < 0.20:
    timing_bonus = 0.0

# Use:
if correlation < 0.20:
    corr_factor = 0.0
elif correlation < 0.40:
    corr_factor = correlation / 0.40  # scales 0.0 → 0.5
else:
    corr_factor = 1.0

timing_bonus = raw_timing_bonus * corr_factor
```

Also check if `lag_mag=+0.00%` means BTC actually didn't move (spike detection threshold might be too high).

---

## Bug 3: `outside_entry_window` = 16-17 Skips Per Cycle

**Root cause: Scanner latency**

Scanner cycle end-to-end: 8+ seconds (sync phase 3.4s + bulk feed 3.4s + scan 8.3s).
Markets with `mins_left=1.6` (1 min 40 sec) are past the entry window before strategy evaluation finishes.

From ops_json:
```
mins_left: n=17, p25=16.60, p50=31.60, p75=61.60, max=121.60
```

Most markets have 16+ minutes left — but the scanner latency burns 8+ seconds that counts against the window.

**Fix options (pick one):**

1. **Async scanner sync** — run scanner in a background thread, strategies read the last cached results instead of waiting for fresh data. Most impactful but complex.

2. **Pre-schedule scanner start** — shift scanner start time earlier in the cycle by accounting for typical sync duration.

3. **Tighten `win_min` in settings.yaml** — exclude markets with <5 minutes left. This is a workaround that throws away some opportunities but prevents false skips.

4. **Add scanner latency to `mins_left` calculation** — subtract observed latency from `mins_left` before comparing to window bounds. Simplest fix.

**Recommended:** Option 4 as quick fix, Option 1 as proper fix.

```python
# In strategy evaluation, before checking entry window:
effective_mins_left = mins_left - scanner_latency_seconds / 60
if not (win_min <= effective_mins_left <= win_max):
    _bump_skip("outside_entry_window")
    continue
```

---

## Verification

After each fix, restart paper trade and check:
1. BTC signals > 0 within 5 cycles
2. `ltf_strength` appears in gate_distributions (n > 0)
3. `outside_entry_window` skips drop from 16 to <5 per cycle
4. Timing bonus shows non-zero when BTC moves even with low correlation

---

## Priority Order

1. **Bug 1 (BTC)** — Most impactful, easy to verify
2. **Bug 3 (scanner latency)** — Affects all strategies
3. **Bug 2 (lag/correlation)** — Lower impact, refine after BTC fires
