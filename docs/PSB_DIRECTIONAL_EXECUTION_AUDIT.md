# Directional execution audit (PSB)

Calibration of win rate / edge vs realized outcomes only makes sense when **both directions** the model implies can be executed.

## ETH macro (`eth_macro`)

- **`direction_source`** in `config/settings.yaml` (`btc` | `hybrid` | `signal_first`).
- **`hybrid`** (default): can resolve **`SHORT`** / `SELL_YES` when 15m implied probability and BTC HTF proxy cross thresholds — see `ETHMacroStrategy._resolve_market_side` in `src/strategies/eth_macro.py`.
- **`signal_first`**: test toggle for direct signal-driven side.
- Before claiming “WR improved” on ETH, confirm **`last_scan_stats.side_source_counts`** (or equivalent logs) show non-`btc_bias` / non-fallback-only paths when the tape is bearish.

## SOL / HYPE / XRP macro (`SolMacroStrategy` family)

- **LONG** and **SHORT** paths exist in the shared macro loop: `BUY_YES` vs `SELL_YES` are driven by **`allowed_side`** from BTC HTF + continuation gates — see `src/strategies/sol_macro.py`.
- Misclassified retrospective losses may still happen from **gates** (e.g. 1H alignment, degraded correlation), not from a single hardcoded `LONG` — distinguish skip stats from “wrong direction.”

## Bitcoin (`bitcoin`)

- Up/down uses **`allowed_side`** + **`action`** `BUY_YES` / `SELL_YES` per hierarchy — not ETH’s former single-side bug pattern.

## Verification checklist

1. ETH `direction_source` is **`hybrid`** or **`signal_first`** when evaluating short-window calibration.
2. For each strategy under review, **`top_skip_reasons`** dominant causes should be understood before attributing PnL to “bad predictions.”
3. Use **`/api/ops/summary`** → **`scan_skip_digest`** for aggregate skip visibility without grepping logs.
