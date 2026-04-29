# Weather Strategy Backtest Script — Specification

## Context

`src/backtest/engine.py` has support for weather strategy (lines 127-130, 322-328) via `BacktestEngine(strategy_name="weather")`, but the existing `run_backtest_crypto.py` script is unsuitable because it:
- Downloads OHLCV candles from Binance (crypto-specific, useless for weather)
- Iterates time-series bars to generate signals (weather strategy reads market lists, not candles)

Weather backtesting requires a fundamentally different approach: **market-by-market**, not bar-by-bar.

---

## What to Build

`scripts/run_backtest_weather.py` — a standalone backtest script for the WeatherStrategy.

### Data Model

For each Polymarket weather market, the backtest needs:
- `question` — e.g., "Will it rain at KLGA airport on April 23?"
- `yes_price` — market price at scan time
- `liquidity` / `volume`
- `end_date` — when market resolves
- `resolution` — actual outcome (True = YES won, rain did happen)

The backtest simulates scanning these markets at a fixed cadence (e.g., once per day or every N hours), checking if the Open-Meteo forecast at that moment has sufficient edge vs the market price.

### Core Logic

For each weather market in the backtest window:
1. **Fetch Open-Meteo forecast** for the airport ICAO + target date at scan time (use `start_date` as proxy for "when the market was listed")
2. **Calculate edge** = `|forecast_prob - market_price| - 0.02` (fee buffer)
3. **If edge >= weather.gap_min (0.15) AND edge >= weather.min_ev (0.05)** → simulate entry
4. **Simulate fill** at market_price (no slippage needed for weather markets — they trade at ~0.50 midpoint typically)
5. **On resolution date**: fetch actual weather from Open-Meteo historical API (or derive from resolution outcome) to determine WIN/LOSS
6. **Record trade**: WIN if (action=BUY_YES and resolution=True) or (action=BUY_NO and resolution=False)

### Key Differences from Crypto Backtest

| | Crypto | Weather |
|---|---|---|
| Data source | Binance OHLCV | Polymarket Gamma + Open-Meteo |
| Granularity | Bar-by-bar (15m/5m) | Market-by-market |
| Resolution | Always resolves (binary) | Weather station data |
| Forecast source | OHLCV indicators | Open-Meteo ensemble |
| Backtest proxy | Assumes 0.50 price | Uses actual market price at scan time |

### Input / Output

**Input flags:**
- `--start YYYY-MM-DD` — backtest start (default: 90 days ago)
- `--end YYYY-MM-DD` — backtest end (default: today)
- `--initial-bankroll FLOAT` — starting bankroll (default: 10000)
- `--min-liquidity FLOAT` — override weather min_liquidity filter
- `--forecast-horizon-days N` — how many days ahead to look for markets (default: 3)

**Output:**
- Console report (P&L, win rate, expectancy, trades)
- JSON saved to `data/backtest/reports/backtest_weather_YYYYMMDD_HHMMSS.json`

### Implementation Steps

1. **Fetch historical weather markets from Polymarket Gamma API**
   - Endpoint: `GET https://gamma-api.polymarket.com/markets`
   - Filter: `active=true`, `closed=false` → but for backtest we need historical, so use `closed=true` and filter by date range
   - Filter by weather keywords (same `_WEATHER_RE` regex from weather.py)
   - Paginate with offset until no more markets
   - Cache to `data/backtest/weather_markets/` as JSON

2. **For each market, build a scan record**
   - `market_id`, `question`, `yes_price`, `no_price = 1 - yes_price`, `liquidity`, `volume`, `end_date`, `resolved` (bool)

3. **Fetch Open-Meteo historical/forecast data**
   - For each market's ICAO + date, call `https://archive-api.open-meteo.com/v1/archive` (historical) or `https://api.open-meteo.com/v1/forecast` (future)
   - For precipitation markets: use `precipitation_probability_max` (already in weather.py)
   - For temperature markets: need to derive probability from ensemble spread (same `_fetch_temp_forecast` logic)
   - Cache to `data/backtest/weather_forecasts/`

4. **Scan and trade simulation**
   - Iterate each market
   - Apply same filters as weather.py (liquidity, hours, gap, EV)
   - For markets with edge signal: record entry at `yes_price` (backtest price)
   - Kelly sizing: same as live, using `kelly_fraction=0.25` and `bankroll`

5. **Resolution**
   - For precipitation: resolved = True if Open-Meteo `precipitation_probability_max > 0.5` at the airport on the target date
   - For temperature: resolved = True if actual temp was within/outside the threshold (for above/below/between markets)
   - WIN = entry action correctly predicted resolution

6. **Report**
   - `BacktestResult` dataclass from engine.py (reused or equivalent)
   - Print summary, save JSON

### Code Style

- Follow the same structure as `run_backtest_crypto.py`
- Use `src/strategies/weather.WeatherStrategy` directly (don't copy logic — call `scan_and_analyze`)
- The weather strategy expects `List[Market]` — construct `Market` objects from historical data
- For backtest mode, set `strategy._backtest_proxy_forecast` to control forecast source
- Use `src.execution.trade_journal.TradeJournal` path conventions for output

### Files to Create/Modify

- **CREATE**: `scripts/run_backtest_weather.py` — main script
- **CREATE**: `data/backtest/weather_markets/` — cache dir for historical market data
- **CREATE**: `data/backtest/weather_forecasts/` — cache dir for fetched forecasts
- No modification to `src/backtest/engine.py` or `src/strategies/weather.py` required

### Vault Handoff

After running, save results to:
`/Users/mainfolder/Documents/Hermes Second Brain/projects/polymarket-bot/strategy-log/weather.md`
Add a new "Backtest Results" entry with: date, period, bankroll, net_pnl, win_rate, trades count, config params used.
