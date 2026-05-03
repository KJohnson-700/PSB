# Pre-registered evaluation tests (PSB)

Write hypotheses **before** changing thresholds or attributing win-rate moves to a specific fix. Record results in the strategy log with real trade counts.

## Example: bearish stack (ETH or macro family)

**Hypothesis:** After directional execution is validated (both LONG and SHORT paths live), when **BTC HTF is BEARISH** and **alt HTF is BEARISH** for **≥3 consecutive scan cycles**, among markets that pass timing and liquidity gates, **`SELL_YES` / NO-side signals** appear at **≥40%** of eligible evaluations in that slice.

- **Falsify if:** rate stays \<20% while skips show `outside_entry_window` only — likely timing, not direction.
- **Adjust thresholds:** Replace 40% with a rate derived from baseline journal data once enough trades exist.

## Example: oracle alignment

**Hypothesis:** When **`skip_on_degraded_correlation`** would trigger but is overridden by config, realized expectancy **does not improve** vs skipped trades (measure over ≥30 trades).

## Record keeping

- Strategy log: `projects/polymarket-bot/strategy-log/<strategy>.md` — append outcomes under **Actual outcome** with **pending** until minimum sample (≥15 closed trades per AGENTS.md).
