# PSB strategic rollout — doc index

Implemented from the integrated strategic plan (ops visibility, evaluation, sizing context).

| Doc | Purpose |
|-----|---------|
| [PSB_DIRECTIONAL_EXECUTION_AUDIT.md](PSB_DIRECTIONAL_EXECUTION_AUDIT.md) | ETH / macro bidirectional execution vs calibration |
| [PSB_PREREGISTERED_TESTS.md](PSB_PREREGISTERED_TESTS.md) | Hypotheses before tuning |
| [PSB_ORACLE_EXCHANGE_POLICY.md](PSB_ORACLE_EXCHANGE_POLICY.md) | Oracle vs exchange as policy |
| [PSB_TIMEZONE_POLICY.md](PSB_TIMEZONE_POLICY.md) | UTC ops clock vs mixed logs |
| [PSB_LIVE_LAST_MILE_CHECKLIST.md](PSB_LIVE_LAST_MILE_CHECKLIST.md) | Live Polymarket checks |
| [PSB_REGIME.md](PSB_REGIME.md) | BTC spot vs break hints (ops) |
| [PSB_PORTFOLIO_RISK.md](PSB_PORTFOLIO_RISK.md) | Stacked correlated exposure |
| [PSB_PAPER_VS_LIVE_FRICTION.md](PSB_PAPER_VS_LIVE_FRICTION.md) | Spread / lag realism |
| [PSB_OPERATOR_BANKROLL_TEMPLATE.md](PSB_OPERATOR_BANKROLL_TEMPLATE.md) | Operator inputs |
| [PSB_KELLY_EXPOSURE_POLICY_TABLE.md](PSB_KELLY_EXPOSURE_POLICY_TABLE.md) | Kelly + exposure mechanics |

**API:** `/api/ops/summary` and live `/api/status` include `scan_skip_digest`, `timestamps_policy`, and optional `regime` (enable under `trading.regime` in `config/settings.yaml`).

**UI:** Command Center shows **Trades today (UTC)** (risk manager daily count), subtitle **fills this session** when available, and a **Top scan skips** line fed from `scan_skip_digest` when the bot is running.
