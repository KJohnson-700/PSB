# Polymarket Backtesting — Research & Tools

This doc ties together the research, GitHub repos, and best practices so we don't repeat basic mistakes.

---

## Critical Rules (from research)

1. **Ruin cap**: You cannot lose more than you have. `final_bankroll >= 0` always.
2. **Three-layer stack**: Signal (prices) → Tradeability (liquidity/spread) → Execution (L2 fills). Don't collapse into one.
3. **No midpoint-fill assumptions**: Midpoint fills overstate edge by 60+ bps. Use L2 order book walk for realistic fills.
4. **Timestamp alignment**: Inner-join prices and metrics. If join drops >5% of rows, investigate before proceeding.
5. **Pin your universe**: Save the exact slug list used. Markets resolve and disappear — results must be reproducible.

---

## GitHub Repos to Use

### 1. [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting)

**Production-grade backtest framework** for Polymarket and Kalshi. Uses NautilusTrader with custom adapters.

- **AccountBalanceNegative**: Stops backtest when balance goes negative (built-in ruin check).
- **PolymarketFeeModel**, slippage, cash-account limits.
- **Settlement-adjusted PnL**: `compute_binary_settlement_pnl` for resolved markets.
- **py-clob-client** for data.
- Requires: Python 3.12+, Rust, uv.

```bash
git clone https://github.com/evan-kolberg/prediction-market-backtesting.git
cd prediction-market-backtesting
uv venv --python 3.13
uv pip install -e nautilus_pm/ bokeh plotly numpy py-clob-client
MARKET_SLUG=will-trump-win-2024 uv run python backtests/polymarket_vwap_reversion.py
```

### 2. [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) (893 stars)

Official Python SDK for Polymarket CLOB. Use for:

- `get_midpoint`, `get_price`, `get_order_book`
- Market discovery, order placement
- Historical data via CLOB `/prices-history`

```bash
pip install py-clob-client
```

### 3. [polymarketdata-sdk](https://pypi.org/project/polymarketdata-sdk/) (paid API)

Official SDK for PolymarketData — 1-min prices, metrics, L2 books. Required for realistic execution modeling per [their blog](https://polymarketdata.co/blog/how-to-backtest-polymarket-strategies-python).

```bash
pip install "polymarketdata-sdk[dataframe]"
```

### 4. [agent-next/polymarket-paper-trader](https://github.com/agent-next/polymarket-paper-trader)

Paper trading + backtest with level-by-level order book execution, exact fee modeling.

### 5. [warproxxx/poly_data](https://github.com/warproxxx/poly_data) (615 stars)

Data pipeline: markets, order events, trades. Outputs `markets.csv`, `orderFilled.csv`, `trades.csv`.

---

## PolymarketData Blog — Step-by-Step

From [How to Backtest Polymarket Strategies with 1-Minute Data](https://polymarketdata.co/blog/how-to-backtest-polymarket-strategies-python):

1. **Universe**: Pin slug list, save `universe_snapshot` with `built_at`, `slugs`, `count`.
2. **Prices + metrics**: Inner-join on `t`. If joined << min(prices, metrics), stop and investigate.
3. **Signal**: Simple, explainable in one sentence. Log `blocked_trade_count`.
4. **Fill simulation**: Walk L2 levels with `weighted_fill()`, use `nearest_book()` for each trade event.
5. **Log every run**: `gross_pnl`, `execution_cost_total`, `net_pnl`, `blocked_trade_count`, `run_id`, `universe_slug_list`.

---

## This Project's Backtest

- **Custom engine** (`src/backtest/engine.py`): Lightweight, uses CLOB prices-history. Has ruin cap and resolution settlement.
- **Nautilus path** (`backtesting/runner.py`): Uses NautilusTrader — has `AccountBalanceNegative` built-in. Requires Nautilus data format.
- **Data sources**: PolymarketLoader (CLOB free), PolymarketDataLoader (paid API), LocalDataLoader (CSV/Parquet).

For production-quality backtests, prefer **evan-kolberg/prediction-market-backtesting** or PolymarketData + L2 fill simulation.

---

## Exit Strategies

Configured in `config/settings.yaml` under `backtest.exit_strategy`:

### `hold_to_settlement` (original)
- Positions held until market resolution.
- Settlement at final price or Gamma API outcome.
- Fees applied on settlement exit.
- Best for: strategies that bet on outcomes (arbitrage, NEH).

### `time_and_target` (current default)
- Checks each bar for exit conditions on open positions:
  - **Time exit**: hours held >= `max_hold_hours` (default 72).
  - **Take profit**: unrealized PnL % >= `take_profit_pct` (default 20%).
  - **Stop loss**: unrealized PnL % <= `-stop_loss_pct` (default 15%).
- Exit fill uses same `_simulate_fill()` (slippage + fees on exit).
- Remaining positions settled at resolution.
- Best for: strategies that trade price moves, not outcomes (fade, consensus).

### Per-Strategy Recommendations
| Strategy | Recommended Exit | Rationale |
|----------|-----------------|-----------|
| Arbitrage | `hold_to_settlement` | Edge is on outcome probability, not price moves |
| Fade | `time_and_target` | Fade consensus on price; exit when price mean-reverts |
| NEH | `hold_to_settlement` | Profits from time decay to 0 |
| Weather | `time_and_target` | Weather forecasts update; exit when gap closes |
| Bitcoin/SOL | Hold to settlement | Window close = UP/DOWN outcome from 1m OHLCV |

---

## Slippage Model

- **Spread-based** (default): `slippage_pct = (spread / 2) * slippage_mult`. Half-spread to cross.
- **BPS fallback**: `slippage_bps / 10_000` when spread unavailable (default 25 bps = 0.25%).
- **Size-dependent scaling**: For sizes > $50, slippage scales by `min(sqrt(size / 50), 2.0)`. A $200 trade gets ~1.4x base slippage.
- **L2 book** (when available): Walks order book levels for VWAP fill. Requires PolymarketData API.

---

## BacktestAIAgent vs Live AIAgent

The backtest uses `src/backtest/backtest_ai.py` — a **deterministic rule-based proxy**, NOT a real LLM.

| Aspect | Live AIAgent | BacktestAIAgent |
|--------|-------------|----------------|
| Provider | OpenAI, Anthropic, Gemini, Groq, MiniMax | Rule-based formulas |
| Confidence | Variable (0-1, model-dependent) | Fixed 0.72 |
| Fade logic | Nuanced analysis with context | `prob = 1 - consensus_price` |
| Arb logic | Deep analysis of market fundamentals | Mean reversion toward 0.5 |
| Cost | API calls ($$) | Free |
| Reproducibility | Non-deterministic | Fully deterministic |

This proxy tests **rule-based entry/exit logic only**. To test AI quality, run live paper trading and compare.

---

## Checklist Before Trusting Results

- [x] Ruin cap: `final_bankroll >= 0`
- [x] Resolution settlement applied (Gamma API or final price)
- [x] Slippage/fees modeled (not midpoint fill) — size-dependent scaling added
- [x] Spread filter blocking logged
- [x] Universe pinned in run metadata
- [ ] Timestamp alignment checked if using multiple data sources
- [x] Exit strategy implemented (`time_and_target` with TP/SL/time)
- [x] Fees applied on both entry and exit (including settlement)
- [x] Real end_date from Gamma API (for NEH strategy)
