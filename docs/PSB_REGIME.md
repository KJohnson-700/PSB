# Regime hints (BTC spot vs breaks)

**Does not gate trades by itself** — informational for ops and correlation with parameter drift.

## Config (`config/settings.yaml`)

```yaml
trading:
  regime:
    enabled: false          # set true to emit regime in OPS_JSON / /api/ops/summary
    btc_break_above_usd: 80000
    btc_break_below_usd: 75000
```

## Ops fields

When **`enabled: true`**, `/api/ops/summary` includes **`regime`** with:

- `btc_spot_usd` — from latest Bitcoin strategy scan stats (`btc_spot_usd`), when available.
- `spot_gte_break_high` / `spot_lte_break_low` — booleans vs configured breaks.

## Future work

- Volatility regime (ATR %, realized vol).
- Alt–BTC correlation regime label for sizing haircuts.
