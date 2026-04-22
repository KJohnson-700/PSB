# PolyBot AI - Polymarket Trading Bot

A hybrid AI-powered trading bot for Polymarket prediction markets.

## Features

### Strategy 1: Auto-Trade Mispriced Markets
- AI analyzes markets and estimates true probability
- Automatically executes trades when edge > threshold
- Uses Kelly Criterion for position sizing

### Strategy 2: Consensus Alerts
- Monitors markets for consensus (>85% probability)
- Alerts user via Discord/Telegram for manual approval
- Does NOT auto-trade - human-in-the-loop

## Project Structure

```
polymarket-bot/
├── config/
│   ├── settings.yaml      # Main configuration
│   └── secrets.env        # API keys (copy from template)
├── src/
│   ├── main.py            # Entry point
│   ├── market/
│   │   ├── scanner.py     # Market data fetching
│   │   └── websocket.py   # Real-time order book
│   ├── analysis/
│   │   ├── ai_agent.py    # LLM decision engine
│   │   └── math_utils.py  # Position sizing
│   ├── strategies/
│   │   ├── arbitrage.py   # Auto-trade strategy
│   │   └── consensus.py   # Alert strategy
│   ├── execution/
│   │   └── clob_client.py # Order execution & risk
│   └── notifications/
│       └── notification_manager.py
├── logs/                  # Log files
└── tests/                 # Test files
```

## Setup

**Python:** `py-clob-client` needs **Python ≥ 3.9.10** (macOS/Linux: **3.11+** recommended). On Windows, use Python 3.11+ from [python.org](https://www.python.org/downloads/) or `py -3.11`. Apple’s Xcode Python 3.9.6 is too old for the CLOB client — use `brew install python@3.11` or a venv with 3.11.

1. **Virtual environment (recommended)**
   ```bash
   python3.11 -m venv .venv
   # Windows: py -3.11 -m venv .venv
   .venv\Scripts\activate     # Windows cmd/PowerShell
   source .venv/bin/activate  # macOS / Linux
   pip install -U pip
   pip install -r requirements-railway.txt   # bot + dashboard (smaller)
   # pip install -r requirements.txt       # full install incl. Nautilus backtests
   ```

2. **Secrets — `.env` at repo root (or `config/secrets.env`)**
   - Put API keys in a **`.env`** file in the project root (typical on Windows), **or** copy the template:
     `cp config/secrets.env.example config/secrets.env` and edit.
   - If both exist, **`config/secrets.env` overrides** the same variable names in `.env`.
   - Keys: [OpenAI](https://platform.openai.com/api-keys) (or your configured LLM provider); Polymarket = Polygon wallet private key + CLOB API credentials from Polymarket.

3. **Configure Settings**
   Edit `config/settings.yaml` to adjust:
   - Trading parameters
   - Strategy thresholds
   - Risk limits
   - Notification settings

## Usage

**Paper sessions, Railway volumes, and journal files** (what persists, how to name/resume sessions, heatmap prerequisites): see [docs/RAILWAY.md](docs/RAILWAY.md#paper-sessions-and-test-data-local--hosted) and [docs/DASHBOARD_DATA_SOURCES.md](docs/DASHBOARD_DATA_SOURCES.md#session-id-and-entriesjsonl-heatmap--analytics).

### Basic Run (Dry Run Mode)
```bash
python start.py              # paper + dashboard (recommended)
# Windows: py -3.11 start.py
python src/main.py --paper
```

If your project path contains **spaces** (e.g. `psb-main 1`), quote the path when you `cd` or invoke the venv: `"/path/to/psb-main 1/.venv/bin/python" start.py`

### With API Keys
```python
# In your code or environment
bot = PolyBot()
bot.set_api_keys(
    openai_key="sk-...",
    polymarket_key="0x..."
)
await bot.start()
```

## Configuration

### Strategy Thresholds

**Arbitrage (Auto-Trade)**
- `min_edge`: 0.10 (10% edge required)
- `ai_confidence_threshold`: 0.70 (70% AI confidence for auto-execute)

**Consensus (Alerts)**
- `threshold`: 0.85 (alert when >85% consensus)
- `min_opposite_liquidity`: $500

### Risk Management
- `max_exposure_per_trade`: 5% of bankroll
- `daily_loss_limit`: 15% stop
- `max_concurrent_positions`: 10

## Discord/Telegram Alerts

To enable notifications, add to `config/secrets.env`:
```
DISCORD_WEBHOOK_URL=your_webhook_url_here
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

## Backtesting

See **[docs/BACKTEST.md](docs/BACKTEST.md)** for research, best practices, and tools.

**Strategy test review** (audit reports and journals for bugs/miscalculations—not pytest CI): **[docs/polymarket-backtest-subagent-skill.md](docs/polymarket-backtest-subagent-skill.md)**.

**Quick run:**
```bash
python scripts/run_backtest.py --strategy fade --slug will-trump-win-2024 --start 2024-10-01 --end 2024-11-14 --bankroll 500 --no-ui
python scripts/run_backtest_multi.py --all --start 2024-10-01 --end 2024-11-30 --bankroll 2000 --target 30 --save-report --no-ui
```

**Production-grade backtesting:** Use [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting) (NautilusTrader + Polymarket adapters, AccountBalanceNegative, L2 fills).

## Top GitHub Repos for Reference

- [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting) - Nautilus backtest for Polymarket/Kalshi
- [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) - Official CLOB SDK (893 stars)
- [nlhx/polymarket-copy-trading-bot](https://github.com/nlhx/polymarket-copy-trading-bot) - 726 stars
- [HyperBuildX/Polymarket-Trading-Bot-Rust](https://github.com/HyperBuildX/Polymarket-Trading-Bot-Rust) - 358 stars (for speed)
- [solship/Polymarket-Kalshi-Arbitrage](https://github.com/solship/Polymarket-Kalshi-Arbitrage-Trading-Bot) - 315 stars

## Disclaimer

This bot is for educational purposes. Trading prediction markets involves substantial risk. Always use dry-run mode first and understand the risks.
