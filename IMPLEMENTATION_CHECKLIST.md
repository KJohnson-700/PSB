# Polymarket Bot Fixes - Detailed Implementation Checklist

This document provides step-by-step instructions for another agent to complete the planned updates. Each task includes file paths, exact code changes, and verification steps.

---

## Task 1: Fix Config Path

**File:** `src/main.py`

**Current behavior:** Default config path may resolve to `src/config/settings.yaml` (non-existent). Config file is at `config/settings.yaml` (project root).

**Change:** In `_load_config`, ensure the default path uses `parent.parent` to reach project root:

```python
# Line ~64: Replace
config_path = Path(__file__).parent / "config" / "settings.yaml"

# With
config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
```

**Verification:** Run `python -c "from pathlib import Path; p = Path('src/main.py').resolve().parent.parent / 'config' / 'settings.yaml'; print('Exists:', p.exists())"` from project root. Should print `Exists: True`.

---

## Task 2: Add fee_buffer to Fade Strategy Config

**File:** `config/settings.yaml`

**Change:** Under `strategies.fade`, add `fee_buffer` (align with arbitrage):

```yaml
  fade:
    enabled: true
    consensus_threshold: 0.95
    ai_confidence_threshold: 0.60
    ipg_min: 0.10
    fee_buffer: 0.02   # ADD THIS LINE
    kelly_fraction: 0.10
    max_strategy_exposure_pct: 0.05
    max_trade_size_pct: 0.01
    time_based_stop_hours: 48
```

**Verification:** Fade strategy should use `self.config.get('fee_buffer', 0.02)` and read 0.02 from config instead of fallback.

---

## Task 3: Fix RiskManager Strategy Config Lookup

**File:** `src/execution/clob_client.py`

**Problem:** `RiskManager` stores only `config.get('risk', {})` but `check_strategy_risk` needs `config.get('strategies', {})` to read fade limits.

**Change:** In `RiskManager.__init__`:

```python
# REPLACE (line ~201-202):
def __init__(self, config: Dict[str, Any]):
    self.config = config.get('risk', {})

# WITH:
def __init__(self, config: Dict[str, Any]):
    self.config = config  # Store full config
    risk_config = self.config.get('risk', {})
```

Then update the risk-specific variables to use `risk_config`:

```python
    self.max_concurrent_positions = risk_config.get('max_concurrent_positions', 10)
    self.max_trades_per_day = risk_config.get('max_trades_per_day', 50)
    self.daily_loss_limit = risk_config.get('daily_loss_limit', 0.15)
    self.emergency_stop_loss = risk_config.get('emergency_stop_loss', 0.25)
```

**Note:** `check_strategy_risk` already uses `self.config.get('strategies', {}).get(strategy_name, {})` — it will work once `self.config` holds the full config.

**Verification:** Add a log in `check_strategy_risk` or verify that changing `max_strategy_exposure_pct` in settings.yaml affects fade trade rejection.

---

## Task 4: Add token_id_yes and token_id_no to Signal Models

### 4a. FadeSignal

**File:** `src/strategies/fade_models.py`

**Change:** Add two fields to `FadeSignal`:

```python
class FadeSignal(BaseModel):
    market_id: str = Field(..., description="The unique identifier for the market.")
    token_id_yes: str = Field(..., description="The token ID for the YES outcome.")
    token_id_no: str = Field(..., description="The token ID for the NO outcome.")
    market_question: str = Field(...)
    # ... rest unchanged
```

### 4b. TradeSignal

**File:** `src/strategies/arbitrage.py`

**Change:** Add two fields to the `TradeSignal` dataclass:

```python
@dataclass
class TradeSignal:
    market_id: str
    token_id_yes: str
    token_id_no: str
    market_question: str
    action: str  # "BUY_YES", "BUY_NO", "SELL_YES"
    # ... rest unchanged
```

---

## Task 5: Populate Token IDs in Strategies

### 5a. FadeStrategy

**File:** `src/strategies/fade.py`

**Change:** When creating `FadeSignal`, pass `market.token_id_yes` and `market.token_id_no`:

```python
signal = FadeSignal(
    market_id=market.id,
    token_id_yes=market.token_id_yes,
    token_id_no=market.token_id_no,
    market_question=market.question,
    action=action,
    # ... rest of fields
)
```

### 5b. ArbitrageStrategy

**File:** `src/strategies/arbitrage.py`

**Change:** In `_generate_signal`, when creating `TradeSignal`, add token IDs:

```python
return TradeSignal(
    market_id=market.id,
    token_id_yes=market.token_id_yes,
    token_id_no=market.token_id_no,
    market_question=market.question,
    action=action,
    price=price,
    # ... rest unchanged
)
```

---

## Task 6: Fix Execution Logic in main.py

### 6a. _execute_arbitrage_signal

**File:** `src/main.py`

**Change:** Replace hardcoded `token_id = "YES_TOKEN"` with logic that uses signal token IDs and handles BUY_NO:

```python
async def _execute_arbitrage_signal(self, signal: TradeSignal):
    """Execute an arbitrage trade signal"""
    logging.info(f"Executing trade: {signal.action} {signal.size} @ {signal.price}")

    # Determine token ID and side based on action
    if signal.action == "BUY_YES":
        token_id = signal.token_id_yes
        side = "BUY"
    elif signal.action == "BUY_NO":
        token_id = signal.token_id_no
        side = "BUY"
    else:  # SELL_YES or fallback
        token_id = signal.token_id_yes
        side = "SELL"

    # Place order
    order = await self.clob_client.place_order(
        token_id=token_id,
        side=side,
        price=signal.price,
        size=signal.size,
        market_id=signal.market_id,
        dry_run=self.config.get('trading', {}).get('dry_run', True)
    )
    # ... rest unchanged
```

### 6b. _execute_fade_signal

**File:** `src/main.py`

**Change:** Replace hardcoded `token_id = "YES_TOKEN"` with signal token ID (fade only trades YES token for BUY_YES/SELL_YES):

```python
    # Determine token ID based on outcome (fade always trades YES token)
    token_id = signal.token_id_yes
    side = "BUY" if signal.action == "BUY_YES" else "SELL"
```

---

## Task 7: Fix FadeStrategy AI Agent Call (If Applicable)

**File:** `src/strategies/fade.py`

**Problem:** Fade calls `ai_agent.analyze_market(market.question)` with one arg, but `AIAgent.analyze_market` expects `(market_question, market_description, current_yes_price, market_id)`.

**Change:** Update the call to match the AI agent signature:

```python
ai_analysis = await self.ai_agent.analyze_market(
    market_question=market.question,
    market_description=market.description,
    current_yes_price=market.yes_price,
    market_id=market.id
)
```

**Also:** AI agent returns `AIAnalysis` (dataclass), not a dict. Update fade to use:
- `ai_analysis.estimated_probability` instead of `ai_analysis['true_probability']`
- `ai_analysis.confidence_score` instead of `ai_analysis['confidence']`

---

## Verification Checklist

After all changes:

1. **Config loads:** Run bot, check logs for "Loaded config from .../config/settings.yaml"
2. **Token IDs:** Ensure no "YES_TOKEN" string remains in main.py
3. **BUY_NO:** Arbitrage BUY_NO signals use token_id_no and side=BUY
4. **RiskManager:** Changing max_strategy_exposure_pct in settings affects fade trades
5. **fee_buffer:** Fade strategy reads fee_buffer from config
6. **Fade AI:** Fade strategy correctly calls AI agent and uses AIAnalysis attributes

---

## File Summary

| File | Tasks |
|------|-------|
| `src/main.py` | 1 (config path), 6a, 6b |
| `config/settings.yaml` | 2 |
| `src/execution/clob_client.py` | 3 |
| `src/strategies/fade_models.py` | 4a |
| `src/strategies/arbitrage.py` | 4b, 5b |
| `src/strategies/fade.py` | 5a, 7 |

---

## Optional: Fade SELL_YES Price Logic

**Status:** Needs verification. Polymarket docs say limit order price is per-share for the token being traded. For SELL_YES, that implies using YES price, not NO price. Current fade uses `market.yes_price ± 0.01` in some versions — confirm this is correct before changing.
