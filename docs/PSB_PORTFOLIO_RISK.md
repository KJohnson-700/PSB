# Portfolio risk — correlated crypto up/down

Multiple strategies (SOL, ETH, HYPE, XRP) can fire on the **same BTC impulse** within one **cycle** or overlapping short windows. Independent **Kelly-sized** entries stack correlated exposure.

## Existing controls (`src/execution/clob_client.py` — `RiskManager`)

- **Crypto bucket:** separate from non-crypto concurrent slots; **`CRYPTO_MAX`** concurrent crypto positions (default 12).
- **Short-term crypto budget:** `evaluate_entry` caps aggregate **SHORT_TERM** crypto exposure vs bankroll (see `crypto_cap` logic).

## Gaps (document / future code)

- **Same resolution window stacking:** two macro legs on the same 5m candle move are still partially correlated — consider **second-leg haircut** or **per-window notional cap** after measuring overlaps from the journal.
- **Explicit portfolio ceiling:** optional global max notional across all open crypto up/down positions.

## Operator action

1. Review **`/api/ops/summary`** → **`scan_skip_digest`** for starvation vs stacking.
2. Set **`bankroll`** and **`max_position_size`** so simultaneous full-tier trades cannot breach session risk tolerance.
