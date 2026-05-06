# Strategy Underperformance Diagnosis

## Summary
- **Baseline sessions:** `test_20260504_034719`
- **Recent sessions:** `test_20260504_150648, test_20260504_195754, test_20260504_220539`
- **Baseline net PnL:** +22.33 across 115 closes
- **Recent net PnL:** -33.41 across 90 closes
- **Recent BUY_YES `updown_time_stop` loss share:** 83.3%
- **Recent `BUY_NO_SKIP` events recorded:** 0

## Live Side Mix

| strategy | baseline BUY_NO share | recent BUY_NO share | baseline TP | recent TP | baseline time_stop | recent time_stop |
|---|---:|---:|---:|---:|---:|---:|
| bitcoin | 20.8% | 0.0% | 26 | 16 | 19 | 10 |
| sol_macro | 10.0% | 0.0% | 14 | 12 | 5 | 8 |
| eth_macro | 0.0% | 0.0% | 7 | 7 | 2 | 9 |
| hype_macro | 0.0% | 0.0% | 12 | 6 | 2 | 6 |
| xrp_macro | 4.3% | 0.0% | 16 | 3 | 7 | 2 |

## Lane Diagnosis

### bitcoin
- **Recent live:** 28 trades, -6.45 PnL, BUY_NO share 0.0%
- **Latest 15m backtest:** `backtest_crypto_BTC_15m_20260505_034718.json` | 193 trades | net -2.48 | WR 51.3%
- **Ranked root causes:**
  - `exit_path_damage` (high) — bitcoin recent BUY_YES updown_time_stop losses were 31.30, 87.3% of negative PnL.
  - `signal_suppression` (medium) — bitcoin BUY_NO share fell from 20.8% to 0.0%, while the latest 15m backtest shows BUY_NO net PnL +48.15 vs BUY_YES -50.62.
- **Next fixes to test:**
  - Replay recent BUY_YES time-stop trades against expiry/relaxed-stop counterfactuals before touching signal gates.
  - Trace BUY_NO admission gaps against the profitable backtest side mix before broadening BUY_YES entries.

### sol_macro
- **Recent live:** 25 trades, +2.48 PnL, BUY_NO share 0.0%
- **Latest 15m backtest:** `backtest_crypto_SOL_15m_20260505_034745.json` | 462 trades | net -403.20 | WR 44.8%
- **Ranked root causes:**
  - `exit_path_damage` (high) — sol_macro recent BUY_YES updown_time_stop losses were 22.88, 82.9% of negative PnL.
  - `entry_quality_or_edge_calibration` (high) — sol_macro latest 15m backtest net PnL was -403.20, with both BUY_NO and BUY_YES negative.
- **Next fixes to test:**
  - Replay recent BUY_YES time-stop trades against expiry/relaxed-stop counterfactuals before touching signal gates.
  - Recalibrate edge thresholds and edge buckets for this lane before assuming missed shorts are the core issue.

### eth_macro
- **Recent live:** 16 trades, -17.32 PnL, BUY_NO share 0.0%
- **Latest 15m backtest:** `backtest_crypto_ETH_15m_20260505_034821.json` | 105 trades | net -99.83 | WR 43.8%
- **Ranked root causes:**
  - `exit_path_damage` (high) — eth_macro recent BUY_YES updown_time_stop losses were 29.53, 100.0% of negative PnL.
  - `entry_quality_or_edge_calibration` (medium) — eth_macro latest 15m backtest net PnL was -99.83, and 3 recent live edge buckets were net negative.
- **Next fixes to test:**
  - Replay recent BUY_YES time-stop trades against expiry/relaxed-stop counterfactuals before touching signal gates.
  - Recalibrate edge thresholds and edge buckets for this lane before assuming missed shorts are the core issue.

### hype_macro
- **Recent live:** 14 trades, -2.70 PnL, BUY_NO share 0.0%
- **Latest 15m backtest:** `backtest_crypto_HYPE_15m_20260501_172912.json` | 5 trades | net +22.43 | WR 80.0%
- **Backtest control quality:** insufficient sample size for strong causal claims
- **Ranked root causes:**
  - `exit_path_damage` (high) — hype_macro recent BUY_YES updown_time_stop losses were 15.15, 77.1% of negative PnL.
- **Next fixes to test:**
  - Replay recent BUY_YES time-stop trades against expiry/relaxed-stop counterfactuals before touching signal gates.
  - Restore a reproducible HYPE 15m backtest dataset first; current live-only evidence is not enough for entry-side changes.

### xrp_macro
- **Recent live:** 7 trades, -9.41 PnL, BUY_NO share 0.0%
- **Latest 15m backtest:** `backtest_crypto_XRP_15m_20260505_034746.json` | 1715 trades | net +283.80 | WR 52.0%
- **Ranked root causes:**
  - `exit_path_damage` (high) — xrp_macro recent BUY_YES updown_time_stop losses were 6.64, 47.5% of negative PnL.
  - `signal_suppression` (medium) — xrp_macro BUY_NO share fell from 4.3% to 0.0%, while the latest 15m backtest shows BUY_NO net PnL +151.88 vs BUY_YES +131.93.
- **Next fixes to test:**
  - Replay recent BUY_YES time-stop trades against expiry/relaxed-stop counterfactuals before touching signal gates.
  - Trace BUY_NO admission gaps against the profitable backtest side mix before broadening BUY_YES entries.
  - Use XRP as the control lane; avoid global architecture rewrites that would discard a still-profitable backtest profile.

## Key Tables

### Recent strategy × action × exit reason

| strategy | action | exit_reason | n | WR | net_pnl | avg_edge | avg_yes_price |
|---|---|---|---:|---:|---:|---:|---:|
| bitcoin | BUY_YES | updown_time_stop | 10 | 0.0% | -31.30 | 0.097 | 0.481 |
| eth_macro | BUY_YES | updown_time_stop | 9 | 0.0% | -29.52 | 0.116 | 0.458 |
| sol_macro | BUY_YES | updown_time_stop | 8 | 0.0% | -22.88 | 0.111 | 0.459 |
| hype_macro | BUY_YES | updown_time_stop | 6 | 0.0% | -15.15 | 0.094 | 0.459 |
| xrp_macro | BUY_YES | RESOLVED:NO (real) | 2 | 0.0% | -7.35 | 0.095 | 0.507 |
| xrp_macro | BUY_YES | updown_time_stop | 2 | 0.0% | -6.64 | 0.130 | 0.430 |
| sol_macro | BUY_YES | RESOLVED:NO (real) | 2 | 0.0% | -4.72 | 0.077 | 0.472 |
| bitcoin | BUY_YES | RESOLVED:NO (real) | 1 | 0.0% | -4.55 | 0.115 | 0.455 |
| hype_macro | BUY_YES | updown_expired | 1 | 0.0% | -4.50 | 0.113 | 0.455 |
| bitcoin | BUY_YES | RESOLVED:YES (real) | 1 | 100.0% | +2.70 | 0.080 | 0.460 |
| xrp_macro | BUY_YES | take_profit | 3 | 100.0% | +4.58 | 0.116 | 0.478 |
| hype_macro | BUY_YES | RESOLVED:YES (real) | 1 | 100.0% | +5.80 | 0.147 | 0.420 |
| sol_macro | BUY_YES | RESOLVED:YES (real) | 3 | 100.0% | +11.10 | 0.130 | 0.440 |
| hype_macro | BUY_YES | take_profit | 6 | 100.0% | +11.15 | 0.120 | 0.471 |
| eth_macro | BUY_YES | take_profit | 7 | 100.0% | +12.20 | 0.126 | 0.466 |
| sol_macro | BUY_YES | take_profit | 12 | 100.0% | +18.98 | 0.089 | 0.463 |
| bitcoin | BUY_YES | take_profit | 16 | 100.0% | +26.70 | 0.099 | 0.481 |

### Recent BUY_NO suppression telemetry

- No `BUY_NO_SKIP` events were present in the selected paper sessions.

## Backtest Controls

| strategy | report | net_pnl | WR | BUY_NO pnl | BUY_YES pnl |
|---|---|---:|---:|---:|---:|
| bitcoin | backtest_crypto_BTC_15m_20260505_034718.json | -2.48 | 51.3% | +48.15 | -50.62 |
| sol_macro | backtest_crypto_SOL_15m_20260505_034745.json | -403.20 | 44.8% | -229.72 | -173.47 |
| eth_macro | backtest_crypto_ETH_15m_20260505_034821.json | -99.83 | 43.8% | -104.40 | +4.58 |
| hype_macro | backtest_crypto_HYPE_15m_20260501_172912.json | +22.43 | 80.0% | +0.00 | +7.88 |
| xrp_macro | backtest_crypto_XRP_15m_20260505_034746.json | +283.80 | 52.0% | +151.88 | +131.93 |
