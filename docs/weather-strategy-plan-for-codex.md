# Weather Strategy — Codex Execution Plan
**Created:** 2026-04-27
**Status:** READY FOR CODEX
**Deadline:** Execute by 11:30 AM Pacific (one-time cron)

---

## CONTEXT

### What Exists
- `src/strategies/weather.py` — WeatherStrategy class (413 lines). Fetches Open-Meteo forecast, compares to Polymarket price, generates WeatherSignal if gap > `gap_min` (default 15¢).
- `scripts/run_backtest_weather.py` — Backtest script (424 lines). Uses PolymarketData API + CLOB to fetch live/closed temperature markets, runs the strategy, outputs JSON report.
- `src/backtest/data_loader.py` — Contains `PolymarketLoader` (Gamma+CLOB free API) and `PolymarketDataLoader` (paid API with `POLYMARKETDATA_API_KEY`).
- Weather is configured `enabled: true` in `config/settings.yaml` at line 81.
- `WeatherStrategy` is instantiated in `PolyBot.__init__` at main.py:190 with only `(config, position_sizer)` — kelly_sizer is passed as `None` (weather.py:70 accepts `kelly_sizer=None`).
- In `_unified_cycle` (main.py:793–815): weather strategy scans `available_markets` (all non-held high-liquidity markets). Weather liquidity threshold is $1,000 (weather_min_liquidity), so it sees a much wider market list than crypto strategies.

### Active Weather Markets (Live Now)
PolymarketData API confirms **288 open city temperature markets** across 18 cities, all expiring April 29, 2026. Examples:
- `highest-temperature-in-manila-on-apr-29-2026`
- `highest-temperature-in-karachi-on-apr-29-2026`
- `highest-temperature-in-dhaka-on-apr-29-2026`
- Plus NYC, London, Tokyo, Seoul, Paris, Dubai, etc.

### Current Problems to Fix

---

## PART 1: FIX PAPER TRADE MARKET FETCHING

### 1A. Fix `_unified_cycle` — Weather Gets WRONG Market List
**File:** `src/main.py`

The weather strategy is fed `available_markets` at line 797, which comes from `_filter_short_horizon(available_markets)`. This filter is designed for CRYPTO markets (15m candle markets that resolve in minutes). For weather markets resolving in 24–72 hours, this filter is wrong.

**Problem:** `_filter_short_horizon` at line 95–105 applies `min_hours=24` and `max_days=14` to ALL non-crypto markets. Weather markets have `min_hours=2` and `max_hours=72` set in the weather config. The general filter strips weather markets that resolve in <24 hours or >14 days, which cuts off valid weather markets.

**Fix:** Create a separate `_filter_weather_markets` function that respects weather's own `min_hours=2.0` and `max_hours=72.0` config. Feed weather strategy its own pre-filtered list, not `available_markets`.

```python
def _filter_weather_markets(markets, config: Dict) -> list:
    """Filter to weather-appropriate markets using weather strategy config."""
    wx_cfg = (config.get("strategies", {}) or {}).get("weather", {})
    min_hours = wx_cfg.get("min_hours_to_resolution", 2.0)
    max_hours = wx_cfg.get("max_hours_to_resolution", 72.0)
    result = []
    for m in markets:
        hours = _hours_to_expiration(m)
        if hours is None:
            continue
        if min_hours <= hours <= max_hours:
            result.append(m)
    return result
```

Then in `_unified_cycle`, call it:
```python
weather_markets = _filter_weather_markets(available_markets, self.config)
weather_signals = await self.weather_strategy.scan_and_analyze(
    markets=weather_markets, bankroll=self.bankroll
)
```

### 1B. Add Dedicated Weather Market Fetcher to Scanner
**File:** `src/market/scanner.py`

Currently scanner has no dedicated weather fetcher. Weather markets are found INCIDENTALLY if they appear in the Gamma API results with enough liquidity. But Polymarket lists weather markets by slug pattern (`highest-temperature-in-{city}-on-{date}`), not by general search.

**Add to `MarketScanner`:**
```python
def fetch_weather_temperature_markets(self, cities: Optional[List[str]] = None) -> List[Market]:
    """Fetch city temperature markets from PolymarketData API.

    These markets follow the slug pattern: highest-temperature-in-{city}-on-{date}
    and are NOT indexed by the general Gamma market list.
    """
    from src.backtest.data_loader import PolymarketDataLoader
    import os, re
    api_key = os.getenv("POLYMARKETDATA_API_KEY")
    if not api_key:
        logger.warning("POLYMARKETDATA_API_KEY not set — weather market fetch skipped")
        return []
    try:
        pm_api = PolymarketDataLoader(api_key=api_key)
        all_markets = pm_api.fetch_markets(search="temperature in", limit=300)
        open_markets = all_markets[all_markets["status"] == "open"]
        result = []
        for _, row in open_markets.iterrows():
            slug = row.get("slug", "")
            # Extract city from slug
            m = re.search(r"highest-temperature-in-(\S+)-on-", slug)
            if not m:
                continue
            city = m.group(1)
            if cities and city not in cities:
                continue
            # Parse end_date
            end_date_str = row.get("end_date", "")
            end_date = None
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).replace(tzinfo=None)
                except:
                    pass
            tokens = row.get("tokens", [])
            result.append(Market(
                id=row.get("marketId", slug),
                question=f"Highest temperature in {city}",
                description=slug,
                volume=float(row.get("volume", 0) or 0),
                liquidity=float(row.get("liquidity", 0) or 0),
                yes_price=0.5,  # Updated by update_market_prices
                no_price=0.5,
                spread=0.02,
                end_date=end_date,
                token_id_yes=tokens[1] if len(tokens) > 1 else "",
                token_id_no=tokens[0] if len(tokens) > 0 else "",
                group_item_title=city,
                slug=slug,
            ))
        return result
    except Exception as e:
        logger.error(f"Weather market fetch error: {e}")
        return []
```

Then in `_sync_network_phase`, add a weather task:
```python
tasks["weather"] = lambda: self.fetch_weather_temperature_markets()
```

And add `weather` to the scan result dict in `_empty_scan_result`.

---

## PART 2: STRATEGY IMPROVEMENTS

### 2A. Pre-Sort Markets by Liquidity (Weather Only)
Weather markets have varying liquidity ($200 to $50K+). The strategy should sort by liquidity descending so it analyzes the biggest markets first. Add to `WeatherStrategy.scan_and_analyze`:

```python
# Sort weather markets by volume desc — analyze biggest markets first
weather_markets.sort(key=lambda m: (m.volume or 0) + (m.liquidity or 0), reverse=True)
```

### 2B. Add Pre-Signal Filter: Volatility Screen
Before calling Open-Meteo, skip markets where the market is already at extreme consensus (yes_price < 0.05 or > 0.95). These have no edge to trade.

```python
if market.yes_price <= 0.05 or market.yes_price >= 0.95:
    stats["skipped_extreme_consensus"] += 1
    continue
```

### 2C. Increase Forecast Horizon Flexibility
Current `forecast_horizon_days: 3` means it only looks at forecasts 3 days out. Many weather markets on Polymarket are "will it rain tomorrow" (T+1). Add a market-by-market horizon:
- If market resolves in <24h: use T+1 forecast
- If market resolves in 1-3 days: use T+2 forecast
- If market resolves in 3+ days: use T+3 forecast

### 2D. Track Scan Statistics Properly
The `WeatherStrategy._scan_stats` dict is updated but the strategy needs to expose it consistently. Ensure all skip reasons are tracked:
- `skipped_no_location` — no ICAO match
- `skipped_no_temp_keyword` — not a temperature market
- `skipped_too_far_out` — forecast horizon too far
- `skipped_below_min_hours` / `skipped_above_max_hours`
- `skipped_extreme_consensus` — price too far from 50¢
- `skipped_below_liquidity` — volume/liquidity too low
- `skipped_no_forecast` — Open-Meteo call failed

---

## PART 3: INTEGRATE WEATHER BACKTEST INTO PSB

### 3A. Fix `run_backtest_weather.py` — CRITICAL BUG

**The backtest was targeting 2024 data which has ZERO weather markets.** The rigorous backtest plan hardcodes 2024, which returned `num_markets: 0` for every quarter.

**FIX:** The backtest MUST run on CURRENT LIVE DATA (April 2026), not 2024.

**Required changes to `run_backtest_weather.py`:**

1. **Remove all 2024 date hardcoding.** The backtest should fetch ALL currently open weather markets from PolymarketData API (April 27-29, 2026) and run the strategy against them as if it were live — entering when the strategy signals and marking PnL based on the actual market resolution (or current price as unrealized PnL if unresolved).

2. **Use the CLOB to get current prices and recent history** for all open weather markets.

3. **The backtest command should be:**
```bash
python scripts/run_backtest_weather.py --save-report
```
NOT with any `--start-date 2024` flag.

4. **Output must show:**
   - How many live markets were found (expect 200+)
   - How many the strategy acted on
   - Current unrealized PnL from open positions
   - Win/loss if any markets have resolved

### 3B. Verify Backtest Uses Current Data

Before running, verify the script does NOT hardcode `start_date="2024-01-01"` anywhere. If it does, remove all 2024 date references. Use today's date or no start date (fetch all available).

### 3B. Also Fix `config/backtest_rigorous.yaml` for Weather

The rigorous backtest config hardcodes 2024 dates which is useless for weather. Weather markets didn't exist on Polymarket in 2024. Codex needs to either:

**Option A:** Add a weather-specific section to `config/backtest_rigorous.yaml`:
```yaml
weather:
  - start: "2026-04-25"
    end: "2026-04-29"
    label: "2026 Apr 26-29 Live"
```

**Option B:** Create a separate `config/backtest_weather.yaml` with the correct dates.

Weather markets exist NOW (April 2026). The backtest should target these live markets, not historical 2024 quarters.

### 3C. Backtest Report Format
```json
{
  "run_date": "2026-04-27T...",
  "markets_total": 288,
  "markets_with_data": <int>,
  "signals_generated": <int>,
  "wins": <int>,
  "losses": <int>,
  "win_rate": <float>,
  "total_pnl": <float>,
  "avg_pnl_per_trade": <float>,
  "by_city": {
    "manila": {"signals": N, "win_rate": X, "pnl": $},
    "karachi": ...
  },
  "signal_details": [...]
}
```

---

## PART 4: VERIFICATION CHECKLIST

Before declaring done, Codex must verify:
- [ ] Weather strategy `scan_and_analyze` runs without error on live Polymarket data
- [ ] Open-Meteo API calls succeed for at least 5 cities (NYC, London, Tokyo, Manila, Karachi)
- [ ] `gap_min` filter fires correctly (should see ~0 signals with gap < 0.15)
- [ ] Weather execution path in `_execute_weather_signal_impl` runs without error (paper mode)
- [ ] Backtest script runs `python scripts/run_backtest_weather.py --quick --save-report` and produces valid JSON
- [ ] JSON report has the fields specified in 3C

---

## FILE CHANGES SUMMARY

| File | Change |
|------|--------|
| `src/main.py` | Add `_filter_weather_markets()`, call it before weather strategy in `_unified_cycle` |
| `src/market/scanner.py` | Add `fetch_weather_temperature_markets()` to MarketScanner, wire into `_sync_network_phase` |
| `src/strategies/weather.py` | Pre-sort by liquidity, add volatility screen, track all skip reasons |
| `scripts/run_backtest_weather.py` | Fix spread fallback, verify PolymarketDataLoader API, add progress logging |
| `config/settings.yaml` | No changes needed — weather config is already correct |

---

## EXECUTION ORDER FOR CODEX

1. Read all affected files: `src/main.py`, `src/market/scanner.py`, `src/strategies/weather.py`, `scripts/run_backtest_weather.py`, `config/settings.yaml`
2. Implement Part 1A: Fix `_filter_short_horizon` weather conflict in `src/main.py`
3. Implement Part 1B: Add `fetch_weather_temperature_markets()` to `src/market/scanner.py`
4. Implement Part 2A–2D: Strategy improvements in `src/strategies/weather.py`
5. Implement Part 3A: Fix issues in `scripts/run_backtest_weather.py`
6. Run `python scripts/run_backtest_weather.py --quick --save-report`
7. Verify output JSON matches spec in Part 3C
8. Report results

---

## PART 5: FIX BTC UP/DOWN MARKET SCANNER (CRITICAL)

### 5A. The Real Problem — Slug Format Changed

**File:** `src/market/scanner.py`

The BTC up/down market slug format on Polymarket has changed from Unix timestamps to human-readable names.

**Old (broken) pattern used by scanner:**
```
btc-updown-15m-1777322700    ← does NOT return markets
btc-updown-5m-1777323000    ← does NOT return markets
```

**Actual live slug format:**
```
bitcoin-up-or-down-april-27-2026-3pm-et   ← WORKS via Gamma /events
bitcoin-up-or-down-april-28-2026-3pm-et
solana-up-or-down-april-27-2026-3pm-et
```

**Verification:** `requests.get('https://gamma-api.polymarket.com/events', params={'slug': 'bitcoin-up-or-down-april-27-2026-3pm-et'})` returns the market with $600K liquidity, $105K volume, and valid outcome prices.

The Gamma `/markets` list endpoint does NOT return these BTC up/down markets — they only appear via the `/events` endpoint with specific human-readable slugs.

### 5B. New Approach — Use Gamma Events List API

Replace the Unix-timestamp slug iteration with a direct Gamma `/events` lookup using a date-based slug pattern.

**Replace `fetch_updown_markets()` (15m) and `fetch_updown_5m_markets()` (5m) with:**

```python
def fetch_updown_markets(self, look_ahead: int = 4) -> List[Market]:
    """Fetch BTC, SOL, ETH, XRP, HYPE Up/Down markets for the next N 15-min windows.

    Polymarket now uses human-readable date slugs:
        bitcoin-up-or-down-april-27-2026-3pm-et
        solana-up-or-down-april-28-2026-3pm-et
        ethereum-up-or-down-april-27-2026-3pm-et
    NOT the old btc-updown-15m-{unix_ts} format.
    """
    markets: List[Market] = []
    now = datetime.now(timezone.utc)
    prefixes = ["bitcoin", "solana", "ethereum", "xrp", "hyperliquid"]

    for offset in range(0, look_ahead + 1):
        # Calculate next 15-min window
        window_time = now + timedelta(minutes=offset * 15)
        # Format: bitcoin-up-or-down-april-27-2026-3pm-et
        date_str = window_time.strftime("%B-%d-%Y").lower()  # april-27-2026
        hour_12 = window_time.strftime("%-I%p").lower()      # 3pm, 4pm (no leading zero)
        time_str = f"{hour_12}-et"                           # 3pm-et
        slug_date = f"{date_str}-{time_str}"                  # april-27-2026-3pm-et

        for prefix in prefixes:
            slug = f"{prefix}-up-or-down-{slug_date}"
            try:
                resp = requests.get(
                    f"{self.GAMMA_API_BASE}/events",
                    params={"slug": slug},
                    timeout=8,
                )
                if resp.status_code != 200:
                    continue
                events = resp.json()
                if not events:
                    continue
                event = events[0]
                for gm in event.get("markets", []):
                    try:
                        # Skip already-resolved
                        outcomes = json.loads(gm.get("outcomePrices", "[]"))
                        yes_price = float(outcomes[0]) if outcomes else 0.5
                        if yes_price <= 0.01 or yes_price >= 0.99:
                            continue

                        tokens = json.loads(gm.get("clobTokenIds", "[]"))
                        vol = float(gm.get("volume", 0) or 0)
                        liq = float(gm.get("liquidity", 0) or 0)
                        end_str = gm.get("endDate") or gm.get("end_date_iso")
                        end_date = None
                        if end_str:
                            try:
                                end_date = datetime.fromisoformat(
                                    end_str.replace("Z", "+00:00")
                                ).replace(tzinfo=None)
                            except (ValueError, TypeError):
                                pass

                        m = Market(
                            id=gm.get("id", ""),
                            question=gm.get("question", ""),
                            description=(gm.get("description", "") or "")[:300],
                            volume=vol,
                            liquidity=liq,
                            yes_price=yes_price,
                            no_price=float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price,
                            spread=abs(yes_price - (1 - yes_price)),
                            end_date=end_date,
                            token_id_yes=tokens[0] if tokens else "",
                            token_id_no=tokens[1] if len(tokens) > 1 else "",
                            group_item_title=gm.get("groupItemTitle", ""),
                            slug=slug,
                        )
                        markets.append(m)
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Failed to fetch updown slug {slug}: {e}")
                continue

    if markets:
        logger.info(
            f"Fetched {len(markets)} 15m updown markets "
            f"(BTC:{sum(1 for m in markets if 'bitcoin' in m.question.lower())}, "
            f"SOL:{sum(1 for m in markets if 'solana' in m.question.lower())})"
        )
    return markets
```

**Replace `fetch_updown_5m_markets()` similarly but use 5-min windows:**

```python
def fetch_updown_5m_markets(self, look_ahead: int = 8) -> List[Market]:
    """Fetch 5-minute BTC & SOL Up/Down markets."""
    markets: List[Market] = []
    now = datetime.now(timezone.utc)
    prefixes = ["bitcoin", "solana", "ethereum", "xrp", "hyperliquid"]

    for offset in range(0, look_ahead + 1):
        window_time = now + timedelta(minutes=offset * 5)
        date_str = window_time.strftime("%B-%d-%Y").lower()
        hour_12 = window_time.strftime("%-I%p").lower()
        time_str = f"{hour_12}-et"
        slug_date = f"{date_str}-{time_str}"

        for prefix in prefixes:
            slug = f"{prefix}-up-or-down-{slug_date}"
            # Same fetch logic as 15m version...
```

### 5C. The 15m/5m Window Slug Problem

**Important:** BTC "Up or Down" markets on Polymarket are NOT grouped by 15-minute windows the way they were in backtests. Instead they are individual hourly/daily resolution windows. Check the actual market at `bitcoin-up-or-down-april-27-2026-3pm-et` — its resolution time will tell you the actual window length.

If the markets are actually 60-minute or "3PM ET resolution" windows (not 15m candles), the `look_ahead` logic may need to iterate by the hour instead. Verify by checking the `endDate` of the fetched market.

### 5D. Update `_resolve_updown_lookahead`

The lookahead resolution (1 hour = 4 windows for 15m, 8 windows for 5m) may need adjustment if Polymarket only creates BTC up/down markets at certain hours (e.g., only at 9AM, 3PM ET). Set `look_ahead=8` for 15m as a safe default to cover 2 hours ahead.

---

## FILE CHANGES SUMMARY (UPDATED)

| File | Change |
|------|--------|
| `src/market/scanner.py` | Replace `fetch_updown_markets()` and `fetch_updown_5m_markets()` with human-readable slug pattern. Update `_resolve_updown_lookahead` if needed. |
| `src/main.py` | Add `_filter_weather_markets()`, call it before weather strategy in `_unified_cycle` |
| `src/strategies/weather.py` | Pre-sort by liquidity, add volatility screen, track all skip reasons |
| `scripts/run_backtest_weather.py` | Fix spread fallback, verify PolymarketDataLoader API, add progress logging |
| `config/settings.yaml` | No changes needed |

---

## CODEX EXECUTION ORDER

1. Read all affected files: `src/market/scanner.py`, `src/main.py`, `src/strategies/weather.py`, `src/strategies/sol_macro.py`, `src/analysis/btc_price_service.py`, `scripts/run_backtest_weather.py`, `config/settings.yaml`
2. Implement Part 6: BTC-as-primary-HTF-gate for alt-coin strategies (DO THIS FIRST — highest impact)
3. Implement Part 5A: Replace `fetch_updown_markets()` and `fetch_updown_5m_markets()` slug patterns in scanner.py
4. Implement Part 1A: Fix `_filter_short_horizon` weather conflict in `src/main.py`
5. Implement Part 1B: Add `fetch_weather_temperature_markets()` to `src/market/scanner.py`
6. Implement Part 2A–2D: Strategy improvements in `src/strategies/weather.py`
7. Implement Part 3A: Fix issues in `scripts/run_backtest_weather.py`
8. Run `python scripts/run_backtest_weather.py --quick --save-report`
9. Verify output JSON matches spec in Part 3C
10. Report results

**IMPORTANT:** Execute in order. Parts 5+1 are independent. Part 6 (BTC primary) must be done before Part 5 to ensure BTC signals work with the fixed scanner.

---

## PART 6: FIX ALT-COIN DIRECTION — BTC AS PRIMARY HTF GATE

### 6A. Root Cause

**Files:** `src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`, `src/strategies/hype_macro.py`, `src/strategies/xrp_macro.py`

**Current architecture (BROKEN):**
- Alt coin strategies use the ALT'S OWN 1H EMA/RSI as the primary direction gate
- BTC correlation is only a secondary ~0.03 probability boost
- Result: ETH's own technicals were bearish, so strategy kept shorting ETH — but BTC was surging and ETH was grinding up. The HTF bias was wrong because it ignored BTC leadership.

**Trade evidence:** 8 ETH trades, all SELL_YES (shorting), 2W/6L = -$37.92. est_prob stuck at 0.39-0.41 while market priced YES at 0.475-0.505. Every market resolved YES (ETH went UP within the 5-15min window).

### 6B. The Fix: BTC 4H as Primary Direction Gate

For SOL, ETH, HYPE, XRP — restructure the layer hierarchy to make BTC the primary signal:

```
LAYER 1 (PRIMARY): BTC 4H trend from BTCPriceService → sets allowed_side
LAYER 2: Alt's own 1H trend → secondary confirmation (does alt agree with BTC?)
LAYER 3: Alt's 15m MACD → confirms alt is following BTC's direction
LAYER 4: Alt's 5m MACD → entry timing
LAYER 5: BTC correlation lag → small boost when alt is catching up to BTC
```

**Key change in `SolMacroStrategy.scan_and_analyze()`:**

The `SOLBTCService` already fetches BOTH BTC and the alt coin from Binance. BTC's 4H trend is available via the `BTCPriceService`. The fix is to fetch BTC's 4H trend and use it as the primary direction gate instead of the alt's own 1H trend.

### 6C. Implementation Steps

**Step 1: Add BTC HTF fetching to `SolMacroStrategy`**

In `__init__`, add:
```python
from src.analysis.btc_price_service import BTCPriceService
self.btc_service = BTCPriceService()
```

**Step 2: Fetch BTC 4H trend in `scan_and_analyze`**

At the top of `scan_and_analyze`, alongside `ta = self.sol_service.get_full_analysis()`, add:
```python
btc_ta = self.btc_service.get_current_analysis()
if btc_ta:
    btc_htf = self._get_btc_htf_bias(btc_ta)
    logger.info(f"BTC HTF: {btc_htf} | BTC ${btc_ta.current_price:,.0f}")
else:
    btc_htf = None
    logger.warning("BTC HTF unavailable — falling back to alt-only analysis")
```

**Step 3: Add `_get_btc_htf_bias()` method**

Copy the BTC 4H bias logic from `BitcoinStrategy._get_higher_tf_bias()` into `SolMacroStrategy`. The method reads:
- `trend_sabre.trend` (1 = bull, -1 = bear)
- `price vs sabre.ma_value`
- `macd_4h` histogram and crossover

This is the same 3-vote system that works for BTC. Apply it as the PRIMARY direction gate for all alt coins.

**Step 4: Replace `allowed_side` source**

Currently:
```python
macro_trend = self._get_macro_trend(ta)  # Uses ALT's 1H trend
allowed_side = "LONG" if macro_trend == "BULLISH" else "SHORT"
```

Change to:
```python
macro_trend = self._get_macro_trend(ta)  # Keep for secondary confirmation
btc_htf = btc_htf or macro_trend  # BTC primary, fall back to alt if BTC unavailable

if btc_htf == "BULLISH":
    allowed_side = "LONG"
elif btc_htf == "BEARISH":
    allowed_side = "SHORT"
else:
    # NEUTRAL: use alt's own HTF as fallback
    allowed_side = "LONG" if macro_trend == "BULLISH" else "SHORT" if macro_trend == "BEARISH" else None

if allowed_side is None:
    logger.info(f"{_brand}: No directional signal (BTC neutral, alt neutral)")
    return []
```

**Step 5: Log both BTC and alt HTF in reason string**

```python
reason_parts.insert(0, f"BTC_HTF={btc_htf}")
reason_parts.insert(1, f"ALT_HTF={macro_trend}")
```

**Step 6: Update adaptive direction gate**

The existing gate at lines 818-832 checks `_h1_trend = mtt.h1_trend` (alt's own 1H) to suppress counter-trend trades. Keep this as-is (it's still useful) but also add BTC check:
```python
# If BTC is strongly bullish and alt is neutral/bearish on 1H, suppress SHORT
if action == "SELL_YES" and btc_htf == "BULLISH":
    _bump_skip("btc_bullish_suppress_short")
    continue
# If BTC is strongly bearish and alt is neutral/bullish on 1H, suppress LONG
if action == "BUY_YES" and btc_htf == "BEARISH":
    _bump_skip("btc_bearish_suppress_long")
    continue
```

**Step 7: ETH, HYPE, XRP inherit the fix**

ETH, HYPE, and XRP all inherit from `SolMacroStrategy` (via `class ETHMacroStrategy(SolMacroStrategy)` etc.). They all use the same `scan_and_analyze()` method. The BTC-primary fix will automatically apply to ALL four alt coin strategies — no changes needed to their individual files.

**Step 8: Keep SOLBTCService lag detection as-is**

The `BTCSOLCorrelation` lag detection (`lag_opportunity`, `opportunity_direction`, `opportunity_magnitude`) should stay as a secondary boost in Layer 5. This part is working correctly and provides value when the alt is catching up to BTC's move.

### 6D. Expected Outcome

| Before | After |
|--------|-------|
| ETH 1H trend = BEARISH → short ETH | BTC 4H trend = BULLISH → only long ETH |
| BTC correlation = 0.03 boost (secondary) | BTC 4H = primary gate |
| 8 trades, 2W/6L, -$37.92 | Should flip to mostly BUY_YES while BTC is bullish |
| est_prob stuck at 0.39 | est_prob should be ~0.55-0.60 when BTC is bullish |

### 6E. Backward Compatibility

The fallback `btc_htf = btc_htf or macro_trend` means:
- If BTCPriceService fails → falls back to alt's own HTF (current behavior)
- If BTC is NEUTRAL → falls back to alt's own HTF
- This preserves existing behavior while adding the BTC-primary layer

---

**Working directory:** `/Users/mainfolder/Documents/psb-main 1/`
**Branch:** current (do not switch)
