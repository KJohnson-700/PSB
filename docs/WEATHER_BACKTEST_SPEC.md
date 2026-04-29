# Weather Strategy Backtest — Implementation Spec
**Date:** 2026-04-27
**Status:** ACTIVE — markets confirmed live on Polymarket

---

## 1. What Exists vs What Needs to Be Built

### Already Done (weather.py)
- `src/strategies/weather.py` — solid: airport ICAO coords, Open-Meteo forecast, METAR, Kelly sizing, gap/EV filters
- `src/strategies/weather_models.py` — WeatherSignal dataclass
- `src/backtest/data_loader.py` — PolymarketLoader (CLOB) + PolymarketDataLoader (paid API)
- `config/settings.yaml` — weather strategy config (enabled: true, gap_min: 0.15, etc.)

### BROKEN — run_backtest_weather.py
Uses `proxy_forecast=0.6` for ALL markets. This is NOT a real backtest.
**Needs complete rewrite.**

---

## 2. Active Weather Markets (Confirmed Live — 2026-04-27)

Source: Browser navigation to polymarket.com → Weather category

### Sub-categories (209 total weather markets)
| Category | Count |
|----------|-------|
| Temperature (daily high) | ~167 |
| Precipitation | 5 |
| Global (temp/anomaly) | 9 |
| Hurricanes | 5 |
| Earthquakes | 12 |
| Tornadoes | 1 |
| Volcanoes | 2 |
| Pandemics | 8 |

### Active Temperature Markets Observed (sample)
All resolve on **airport weather station** METAR data (NOT city center).

| City | Date | Bucket | Market Price (implied prob) | Volume |
|------|------|--------|------------------------------|--------|
| Hong Kong | Apr 28 | 28°C Yes | ~46% | $67K |
| Hong Kong | Apr 28 | 27°C Yes | ~40% | $67K |
| Shanghai | Apr 28 | 20°C Yes | ~39% | $59K |
| Shanghai | Apr 28 | 21°C Yes | ~32% | $59K |
| Dallas | Apr 28 | 86-87°F Yes | ~35% | $56K |
| Dallas | Apr 28 | 84-85°F Yes | ~31% | $56K |
| London | Apr 28 | 16°C Yes | ? | $50K |
| London | Apr 28 | 15°C Yes | ? | $50K |
| Seoul | Apr 28 | 16°C Yes | ? | high |
| Seoul | Apr 28 | 15°C Yes | ? | high |

Additional cities likely: Tokyo, Chicago, NYC, Miami, Singapore, Dubai, Paris, Sydney, Seattle, Atlanta, Houston, Denver, Phoenix, Boston, Manila, Karach

**Date range available:** Apr 27, Apr 28, Apr 29 (date selector buttons on page)

### Market Structure
- Each city has MULTIPLE binary markets (one per 1°C or 1°F bucket)
- Markets are "Will the highest temp in [CITY] on [DATE] be between [X-Y]°?"
- Resolution: actual max temp at airport weather station (ICAO) on that date
- Volume: $50K-$67K for top markets, substantial for most

---

## 3. Backtest Architecture

### Data Flow
```
Closed Weather Markets
    │
    ├─ PolymarketLoader (CLOB) → price history (start→end_date, 1h fidelity)
    │                              Problem: CLOB only keeps 12h+ granularity for closed markets
    │
    ├─ PolymarketDataLoader (paid API) → price history + order book
    │      Problem: requires POLYMARKETDATA_API_KEY
    │
    └─ Resolution Outcomes
           ├─ Gamma API: get_resolution_outcome(slug) → True/False
           └─ Or: fetch closed market from Gamma, check outcomePrices
```

### Entry Signal (same as live weather.py)
At each price-history timestamp T:
1. Fetch Open-Meteo forecast for airport station → forecast_prob
2. Compare to market YES price at time T → gap
3. If gap > gap_min (0.15) AND forecast_prob > min_ev (0.05) → signal
4. Kelly-size position

### Exit / Resolution
- Outcome: fetch actual max temp from Open-Meteo for airport station on resolution date
- Or: use Gamma API `get_resolution_outcome()` which returns True/False from on-chain result
- PnL = size × (exit_price − entry_price) for YES position
  - exit_price = 1.0 if won, 0.0 if lost

### Position Sizing
- Kelly fraction: 0.25 (from config)
- Edge = forecast_prob − market_price − 0.02 (fee buffer)
- size = Kelly(bankroll, edge, fraction)

### Filters
- min_liquidity: $1000 (from config)
- min_hours_to_resolution: 2h (from config)
- max_hours_to_resolution: 72h (from config)
- max_yes_price: 0.45 (don't pay more than 45¢ for YES)

---

## 4. Files to Create/Modify

### New Files
1. `src/backtest/weather_backtest_engine.py` — Core backtest engine
   - `WeatherBacktestEngine` class
   - `run(markets, start_date, end_date, bankroll)` → `WeatherBacktestResult`
   - Historical Open-Meteo data fetch for resolution verification
   - Walk price history at each price tick, generate signals
   - Resolve trades from actual weather outcomes

2. `scripts/run_weather_backtest.py` — NEW cleaner entry point
   - Fetches closed weather markets (last 30-60 days) from PolymarketDataLoader
   - For each market: fetches price history + resolves actual outcome
   - Runs backtest engine
   - Outputs: win rate, total PnL, expectancy, Kelly-optimal vs real returns

3. `src/backtest/weather_resolution.py` — Resolution helper
   - `fetch_actual_max_temp(icao, date)` → float (°F)
   - `resolve_market(slug)` → True/False from Gamma or Open-Meteo
   - Caches results to avoid re-fetching

### Files to Modify
1. `scripts/run_backtest_weather.py` — Replace with reference to new engine
   - Keep it minimal, just calls `WeatherBacktestEngine`
   - Add `--strategy-only` flag to run with synthetic forecast (for signal testing)

2. `src/backtest/engine.py` — Add weather strategy support (optional future)

---

## 5. Key Design Decisions

### Temperature Market Resolution
Polymarket resolves on "highest temperature at [airport ICAO] on [date]"
- Use Open-Meteo `daily` API: `temperature_2m_max` for that airport's lat/lon
- Compare to bucket threshold to determine YES/NO outcome

### Price History Fidelity
- CLOB: 12h+ granularity for closed markets (documented limitation)
- PolymarketDataLoader (paid): better resolution
- **Accept 12h fidelity for backtest** — weather markets move slowly, 12h is acceptable

### Bucket Outcome Calculation
For a market "Will temp be between 28-29°C?":
1. Get actual max temp from Open-Meteo for airport on date
2. YES won = actual ∈ [28, 29]
3. Also compute actual temp to compare to forecast

### Signal Generation at Each Price Point
Walk price history. At each timestamp:
- Create "snapshot market" with current YES price
- Fetch Open-Meteo forecast for target date
- Compare: gap > 0.15? → generate signal
- Apply Kelly sizing, record trade

### Backtest Result
```python
@dataclass
class WeatherBacktestResult:
    total_markets: int
    markets_with_signals: int
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_edge: float
    expectancy: float  # $ per trade
    kelly_optimal_pnl: float
    real_pnl: float
    by_city: Dict[str, CityStats]
```

---

## 6. City→ICAO→Open-Meteo Mapping

Already in `weather.py`:
```python
new york / nyc / laguardia / jfk → KLGA/KJFK (40.7769, -73.8740)
los angeles / lax → KLAX (33.9425, -118.4081)
chicago / o'hare → KORD (41.9742, -87.9073)
miami → KMIA (25.7959, -80.2870)
dallas → KDAL (32.8481, -96.8512)
denver → KDEN (39.8561, -104.6737)
phoenix → KPHX (33.4342, -112.0116)
boston → KBOS (42.3656, -71.0096)
seattle → KSEA (47.4502, -122.3088)
atlanta → KATL (33.6407, -84.4277)
houston → KIAH (29.9902, -95.3368)
london / heathrow → EGLL (51.4700, -0.4543)
tokyo → RJTT (35.5494, 139.7798)
seoul → RKSS (37.4691, 126.4510)
paris → LFPB (49.0097, 2.5479)
sydney → YSSY (-33.9399, 151.1753)
singapore → WSSS (1.3644, 103.9915)
dubai → OMDB (25.2532, 55.3657)
```

**Additional cities from current Polymarket:**
- hong-kong → VHHH (22.3089, 113.9184)
- shanghai → ZSPD (31.1443, 121.8083)
- manila → RPLL (14.5086, 121.0197)
- karachi → OPKC (24.8933, 67.1681)
- new delhi → VIDP (28.5562, 77.1000)
- mumbai → VABB (19.0883, 72.8679)
- bangkok → VTBS (13.6900, 100.7501)

---

## 7. Implementation Order

1. **Phase 1:** Weather backtest engine (core logic)
   - `weather_resolution.py` — fetch actual temps + resolve markets
   - `weather_backtest_engine.py` — walk prices, generate signals, compute PnL
   - `run_weather_backtest.py` — CLI, fetch closed markets, run engine

2. **Phase 2:** Closed market data fetch
   - Use Gamma API to list all closed temperature markets (last 30 days)
   - Filter by volume > $10K
   - For each: fetch price history + resolve outcome

3. **Phase 3:** Validation
   - Run on 30 days of data
   - Report by city, by bucket width, by lead time
   - Compare Kelly-optimal vs real PnL
