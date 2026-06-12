# Portfolio risk — correlated crypto up/down

Multiple strategies (SOL, ETH, HYPE, XRP) can fire on the **same BTC impulse** within one **cycle** or overlapping short windows. Independent **Kelly-sized** entries stack correlated exposure.

## Existing controls (`src/execution/clob_client.py` — `RiskManager`)

- **Global position cap:** all active PSB execution lanes are crypto up/down, so `max_concurrent_positions` applies to the whole bot.
- **Term budget:** `evaluate_entry` caps aggregate exposure by market term using `term_risk.caps` from config. Short-window crypto markets normally consume the **SHORT_TERM** cap.

## Gaps (document / future code)

- **Same resolution window stacking:** two macro legs on the same 5m candle move are still partially correlated — consider **second-leg haircut** or **per-window notional cap** after measuring overlaps from the journal.
- **Explicit per-resolution ceiling:** optional max notional per exact expiry bucket, because all active lanes can stack into the same short-window resolution.

## Operator action

1. Review **`/api/ops/summary`** → **`scan_skip_digest`** for starvation vs stacking.
2. Set **`bankroll`** and **`max_position_size`** so simultaneous full-tier trades cannot breach session risk tolerance.
