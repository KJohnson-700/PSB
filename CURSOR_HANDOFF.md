# Cursor Handoff — ETH Lag Strategy + Z-Score Filter + Live Scan Fix

**Date:** 2026-04-09  
**Branch:** main  
**Implements:** ETH lag strategy by generalizing existing SOL lag architecture

> **Naming (2026-04):** Production identifiers are **`sol_macro`**, **`eth_macro`**, **`hype_macro`**, **`xrp_macro`** — modules `src/strategies/*_macro.py`, classes `*MacroStrategy`, executor **`_execute_sol_macro_signal`** / **`_execute_xrp_macro_signal`**. Sections below may still say “lag” or older symbol names; treat them as the same architecture.

---

## Context

The bot currently runs BTC + SOL updown strategies. ETH updown markets are confirmed live on Polymarket (`eth-updown-15m-{unix_ts}`). ETH has ~0.85 BTC correlation vs SOL's ~0.74 — strong candidate for the same architecture. The SOL lag service needs to be generalized first, then ETH is a thin subclass.

Also fixing:
- BTC spike detection uses fixed thresholds — replace with Z-score (adaptive to vol regime)
- Live scan dashboard tests disabled strategies — add updown market status section
- `_execute_sol_macro_signal` in main.py hardcodes "sol_macro" string — parameterize it

---

## Implementation Order

### Step 1 — Generalize `src/analysis/sol_btc_service.py`

**Add `alt_symbol` init param:**
```python
def __init__(self, polygon_rpc: str = None, alt_symbol: str = "SOLUSDT"):
    self.alt_symbol = alt_symbol
    self.spike_z_threshold = 1.5   # Z-score threshold for spike detection
    # ... rest of existing init unchanged
```

**Replace all 7 hardcoded `"SOLUSDT"` literals** with `self.alt_symbol`:
- Line 386: `df_15m = self.fetch_klines("SOLUSDT", "15m", 200)` → `self.alt_symbol`
- Line 387: `df_5m = self.fetch_klines("SOLUSDT", "5m", 100)` → `self.alt_symbol`
- Line 388: `df_1h = self.fetch_klines("SOLUSDT", "1h", 100)` → `self.alt_symbol`
- Line 487: `df_sol_1m = self.fetch_klines("SOLUSDT", "1m", 60)` → `self.alt_symbol`
- Line 634: `df_1d = self.fetch_klines("SOLUSDT", "1d", 60)` → `self.alt_symbol`
- Line 680: `df_1h = self.fetch_klines("SOLUSDT", "1h", 100)` → `self.alt_symbol`
- Line 696: `df_15m = self.fetch_klines("SOLUSDT", "15m", 100)` → `self.alt_symbol`
- Line 713: `df_5m = self.fetch_klines("SOLUSDT", "5m", 100)` → `self.alt_symbol`

Update any log lines that say `"SOL $xxx"` to use `self.alt_symbol.replace("USDT", "")` so ETH service logs correctly.

**Backward compatible**: `SOLBTCService()` with no args still works — SOL strategy unaffected.

---

### Step 2 — Replace fixed spike thresholds with Z-score in `calc_correlation()`

**File:** `src/analysis/sol_btc_service.py`

Find lines ~528-536 (the spike detection block):
```python
spike_5m = abs(result.btc_move_5m_pct) > 0.3
spike_15m = abs(result.btc_move_15m_pct) > 0.8
```

Replace with adaptive Z-score using `df_btc_1m` already in scope (60 bars of 1m BTC data):

```python
# --- Z-score adaptive spike detection ---
_z_threshold = self.spike_z_threshold  # 1.5 by default

_closes = df_btc_1m["close"].values  # length = 60

# Build rolling samples of 5m and 15m absolute % moves
_moves_5m = [abs((_closes[i] - _closes[i-5]) / _closes[i-5] * 100) for i in range(5, len(_closes))]
_moves_15m = [abs((_closes[i] - _closes[i-15]) / _closes[i-15] * 100) for i in range(15, len(_closes))]

# Use last 20 samples excluding current bar for rolling window
_window_5m = _moves_5m[-21:-1] if len(_moves_5m) >= 21 else _moves_5m[:-1]
_window_15m = _moves_15m[-21:-1] if len(_moves_15m) >= 21 else _moves_15m[:-1]

spike_5m = False
spike_15m = False

if len(_window_5m) >= 5:
    _mean_5m = float(np.mean(_window_5m))
    _std_5m = float(np.std(_window_5m))
    _current_5m = abs(result.btc_move_5m_pct)
    spike_5m = ((_current_5m - _mean_5m) / _std_5m > _z_threshold) if _std_5m > 0.01 else (_current_5m > 0.3)
else:
    spike_5m = abs(result.btc_move_5m_pct) > 0.3  # fallback

if len(_window_15m) >= 5:
    _mean_15m = float(np.mean(_window_15m))
    _std_15m = float(np.std(_window_15m))
    _current_15m = abs(result.btc_move_15m_pct)
    spike_15m = ((_current_15m - _mean_15m) / _std_15m > _z_threshold) if _std_15m > 0.01 else (_current_15m > 0.8)
else:
    spike_15m = abs(result.btc_move_15m_pct) > 0.8  # fallback
```

`np` is already imported in this file. No other downstream changes needed — `spike_5m` and `spike_15m` are used identically after this block.

---

### Step 3 — Add `strategy_name` to `SolMacroSignal` in `src/strategies/sol_macro.py`

Find the `SolMacroSignal` Pydantic model (around line 46-80). Add one field:

```python
strategy_name: str = "sol_macro"
```

---

### Step 4 — Create `src/strategies/eth_macro.py` (new file)

```python
"""
ETH Lag Strategy — BTC-to-Ethereum correlation lag trading.

Inherits SolMacroStrategy's full 5-layer architecture.
Only overrides: service symbol, market filter patterns, config key.
"""
import re
from typing import Dict, Any

from src.strategies.sol_macro import SolMacroStrategy
from src.analysis.sol_btc_service import SOLBTCService
from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market

ETH_PATTERNS = [
    re.compile(r'\bethereum\b', re.IGNORECASE),
    re.compile(r'\beth\b', re.IGNORECASE),
    re.compile(r'\bether\b', re.IGNORECASE),
]
ETH_UPDOWN_PATTERN = re.compile(r'(?:ethereum|eth|ether)\s+up\s+or\s+down', re.IGNORECASE)


class ETHMacroStrategy(SolMacroStrategy):
    """ETH Lag strategy — identical architecture to SOL lag, different asset."""

    def __init__(self, config: Dict[str, Any], ai_agent: AIAgent,
                 position_sizer: PositionSizer, exposure_manager: ExposureManager = None):
        super().__init__(config, ai_agent, position_sizer, exposure_manager)
        # Override config key to read from strategies.eth_macro
        self.config = config.get('strategies', {}).get('eth_macro', {})
        self.enabled = self.config.get('enabled', False)
        # Override service to use ETHUSDT
        self.sol_service = SOLBTCService(alt_symbol="ETHUSDT")
        # Re-read config values that __init__ already set from sol_macro config
        self.min_liquidity = self.config.get('min_liquidity', 1000)
        self.min_edge = self.config.get('min_edge', 0.06)
        self.min_edge_5m = self.config.get('min_edge_5m', self.min_edge)
        self.kelly_fraction = self.config.get('kelly_fraction', 0.15)
        self.entry_price_min = self.config.get('entry_price_min', 0.46)
        self.entry_price_max = self.config.get('entry_price_max', 0.54)

    def _is_solana_market(self, market: Market) -> bool:
        """Override: detect ETH markets instead of SOL."""
        text = f"{market.question} {market.description}".lower()
        has_eth = any(p.search(text) for p in ETH_PATTERNS)
        is_btc_only = 'bitcoin' in text and 'ethereum' not in text and 'eth' not in text
        return has_eth and not is_btc_only

    def _is_updown_market(self, market: Market) -> bool:
        """Override: detect ETH Up or Down markets."""
        return bool(ETH_UPDOWN_PATTERN.search(market.question))
```

---

### Step 5 — Add ETH slugs to `src/market/scanner.py`

Find `fetch_updown_markets()` (line ~264):
```python
for prefix in ["btc-updown-15m", "sol-updown-15m"]:
```
Change to:
```python
for prefix in ["btc-updown-15m", "sol-updown-15m", "eth-updown-15m"]:
```

Find `fetch_updown_5m_markets()` (line ~353):
```python
for prefix in ["btc-updown-5m", "sol-updown-5m"]:
```
Change to:
```python
for prefix in ["btc-updown-5m", "sol-updown-5m", "eth-updown-5m"]:
```

Update the log summary lines to count ETH markets. Zero risk — missing slugs are handled by existing `if not events: continue` guard.

---

### Step 6 — Parameterize `_execute_sol_macro_signal` in `src/main.py`

Find `_execute_sol_macro_signal()` (line ~1178). It has 7 hardcoded `"sol_macro"` strings. Replace each with `signal.strategy_name` (which defaults to `"sol_macro"` from Step 3, so SOL behavior is unchanged).

Key lines to update (search for `"sol_macro"` inside this method):
- `can_trade, reason = self.risk_manager.can_trade(strategy="sol_macro")`  → `strategy=signal.strategy_name`
- All `strategy="sol_macro"` in journal/position calls → `strategy=signal.strategy_name`

Also update the resolution check filter at line ~573:
```python
s.get("strategy") in ("bitcoin", "sol_macro")
```
→
```python
s.get("strategy") in ("bitcoin", "sol_macro", "eth_macro")
```

---

### Step 7 — Wire ETH strategy in `src/main.py`

**Imports** (add near SolMacroStrategy import):
```python
from src.strategies.eth_macro import ETHMacroStrategy
```

**In `__init__`** (after SOL strategy init):
```python
self.eth_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
self.eth_macro_strategy = ETHMacroStrategy(
    self.config,
    self.ai_agent,
    self.position_sizer,
    exposure_manager=self.eth_exposure_manager,
)
```

Add `"eth_macro": 0` to `self.last_signal_counts` and `self.cumulative_signal_counts`.

**In `_crypto_cycle()`** (after SOL block, ~line 525):
```python
# --- ETH Lag ---
eth_macro_cfg = self.config.get('strategies', {}).get('eth_macro', {})
if eth_macro_cfg.get('enabled', False):
    try:
        self.eth_macro_strategy._open_positions_snapshot = list(
            self.risk_manager.active_positions.values()
        )
        eth_signals = await self.eth_macro_strategy.scan_and_analyze(
            markets=short_horizon, bankroll=self.bankroll
        )
        self.last_signal_counts["eth_macro"] = len(eth_signals)
        self.last_cycle_times["eth_macro"] = _now_iso
        self.cumulative_signal_counts["eth_macro"] = (
            self.cumulative_signal_counts.get("eth_macro", 0) + len(eth_signals)
        )
        for signal in eth_signals:
            await self._execute_sol_macro_signal(signal)
        logging.info(
            f"[FAST] Crypto ETH: {len(eth_signals)} signal(s)" if eth_signals
            else "[FAST] Crypto ETH: No signals this cycle"
        )
    except Exception as e:
        logging.error(f"Crypto ETH cycle error: {e}", exc_info=True)
```

**In exposure manager routing** (find `elif strategy == "sol_macro":` and add after):
```python
elif strategy == "eth_macro":
    return self.eth_exposure_manager
```

---

### Step 8 — Add `eth_macro` config to `config/settings.yaml`

Add after the `sol_macro:` block:

```yaml
  eth_macro:
    enabled: true
    use_ai: true
    min_liquidity: 1000
    min_edge: 0.06
    min_edge_5m: 0.06
    max_edge_updown: 0.15
    ai_confidence_threshold: 0.60
    max_ai_calls_per_scan: 8
    kelly_fraction: 0.15
    entry_price_min: 0.46
    entry_price_max: 0.54
    min_lag_magnitude_pct: 0.30
    blocked_utc_hours_updown: [18, 22]
    look_ahead_5m: 8
    max_concurrent_positions: 3
    entry_window_15m_min: 12.0
    entry_window_15m_max: 14.5
    entry_window_5m_min: 2.5
    entry_window_5m_max: 4.0
    btc_min_move_dollars_15m: 40.0
    btc_min_move_dollars_5m: 25.0
```

---

### Step 9 — Fix live scan in `scripts/live_strategy_scan.py`

Add a new function `scan_updown_markets(config)` and call it from `main()` after the existing scan summary. The function should:

1. Instantiate `MarketScanner` and call `fetch_updown_markets(look_ahead=4)` and `fetch_updown_5m_markets(look_ahead=8)`
2. Group results by asset (BTC/SOL/ETH) and print market question, YES/NO prices, liquidity, expiry
3. Instantiate `SOLBTCService()` and call `get_full_analysis()` to print current signal state (trend direction, BTC spike status, lag opportunity, correlation)

```python
def scan_updown_markets(config):
    """Print live status of BTC/SOL/ETH updown markets + current signal state."""
    from src.market.scanner import MarketScanner
    from src.analysis.sol_btc_service import SOLBTCService

    print("\n" + "=" * 70)
    print("UPDOWN MARKET STATUS (BTC / SOL / ETH)")
    print("=" * 70)

    scanner = MarketScanner(config)

    markets_15m = scanner.fetch_updown_markets(look_ahead=4)
    markets_5m = scanner.fetch_updown_5m_markets(look_ahead=8)
    all_updown = markets_15m + markets_5m

    print(f"\nFetched {len(markets_15m)} 15m + {len(markets_5m)} 5m updown markets")

    if not all_updown:
        print("  No updown markets in current window — slugs may not exist yet.")
    else:
        for keyword, label in [("bitcoin", "BTC"), ("solana", "SOL"), ("ethereum", "ETH")]:
            asset_mkts = [m for m in all_updown if keyword in m.question.lower()]
            if not asset_mkts:
                print(f"\n  {label}: No markets found")
                continue
            print(f"\n  {label} ({len(asset_mkts)} markets):")
            for m in asset_mkts:
                print(f"    {m.question[:65]}")
                print(f"      YES={m.yes_price:.3f}  NO={m.no_price:.3f}  liq=${m.liquidity:,.0f}")

    print("\n\nSOL/BTC SIGNAL STATE")
    print("-" * 70)
    try:
        svc = SOLBTCService()
        ta = svc.get_full_analysis()
        if ta:
            corr = ta.correlation
            mtt = ta.multi_tf
            sol = ta.sol
            print(f"  SOL: ${sol.current_price:.2f}  Trend={mtt.overall_direction}  "
                  f"1H={mtt.h1_trend}  15m={mtt.m15_trend}  5m={mtt.m5_trend}")
            print(f"  BTC: ${corr.btc_price:,.0f}  5m={corr.btc_move_5m_pct:+.2f}%  "
                  f"15m={corr.btc_move_15m_pct:+.2f}%  spike={corr.btc_spike_detected}")
            print(f"  Lag opp: {corr.lag_opportunity}  dir={corr.opportunity_direction}  "
                  f"mag={corr.opportunity_magnitude:+.2f}%  corr1h={corr.correlation_1h:.3f}")
        else:
            print("  Could not fetch analysis — Binance may be rate-limiting")
    except Exception as e:
        print(f"  Error: {e}")
```

---

## Verification After Implementation

```bash
# 1. Verify ETH service works
python -c "from src.analysis.sol_btc_service import SOLBTCService; svc = SOLBTCService('ETHUSDT'); ta = svc.get_full_analysis(); print('ETH price:', ta.sol.current_price)"

# 2. Verify ETH strategy instantiates
python -c "from src.strategies.eth_macro import ETHMacroStrategy; print('ETH strategy OK')"

# 3. Verify live scan works
python scripts/live_strategy_scan.py

# 4. Check Railway logs after deploy for:
# [FAST] Crypto ETH: No signals this cycle    ← strategy running
# ETH ($X.XX): X markets                      ← markets found
```

---

## Commit message to use
```
Add ETH lag strategy + Z-score spike filter + live scan fix

- Generalize SOLBTCService with alt_symbol param (SOLUSDT default)
- Replace fixed BTC spike thresholds with adaptive Z-score (1.5σ rolling 20-bar)
- Add ETHMacroStrategy subclass — inherits all 5 SOL lag layers unchanged
- Add eth-updown-15m/5m slugs to scanner (confirmed live on Polymarket)
- Parameterize _execute_sol_macro_signal to use signal.strategy_name
- Add eth_macro config block; wire strategy in main.py crypto cycle
- Fix live scan: add updown market status section to live_strategy_scan.py
```
