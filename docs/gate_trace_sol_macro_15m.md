# SOL macro: 15m up/down gate order and payoff mapping

Reference implementation: [`sol_macro.py`](../src/strategies/sol_macro.py) (`scan_and_analyze`, updown branch).

## Payoff mapping vs Polymarket

| Code | Polymarket contract (typical `sol-updown-15m-*`) |
|------|--------------------------------------------------|
| `allowed_side == "LONG"` → `action = "BUY_YES"`, `direction = "UP"` | YES pays if spot **closes up** vs window open (oracle / resolution rules on the market). |
| `allowed_side == "SHORT"` → `action = "BUY_NO"`, `direction = "DOWN"` | NO pays if spot **does not** close up (i.e. flat or down vs reference). |

Features (MACD, BTC lag) are **trading signals for that payoff**; they must not invert YES/NO. Chainlink/oracle basis gates align live feed vs resolution where configured.

## Ordered gates (15m up/down, per scan)

Pre-loop macro and LTF policy (~934–1098): `allowed_side` from BTC HTF / neutral branches; `ltf_confirmed`, `ltf_strength`, `ltf_reasons` from `_check_15m_confirmation`; optional `skip_15m_reason` skips all non-5m updown for the cycle.

Per market, approximate order:

1. Liquidity (`liquidity`)
2. `skip_15m_reason` if 15m updown (`ltf_required_unconfirmed_15m`, `anti_ltf_confirmed_15m`, …)
3. Dead-zone UTC hour (`blocked_utc_hour`)
4. `end_date` present (`missing_end_date`)
5. Entry window vs `mins_left` / latency (`outside_entry_window`)
6. BTC min dollar move (`btc_min_move_dollars`)
7. YES price band 0.20–0.80 (`price_too_far_from_even`)
8. Map side → `BUY_YES` / `BUY_NO` (payoff mapping, ~1272–1278)
9. Degraded correlation (`degraded_correlation`)
10. BTC volatility gate (`flat_btc_no_lag`)
11. Alt 1H alignment Macd overrides (`sell_yes_suppressed_bullish_1h`, `buy_yes_suppressed_bearish_1h`)
12. RSI hard / soft (soft adjusts prob; not a skip — see telemetry key `rsi_soft_penalty` sample only)
13. Oracle basis (`oracle_basis_block`)
14. **IQL** `_passes_15m_iql` — shares `_check_15m_confirmation` “confirmed” path, then relaxed cross/hist floor (`iql_15m_reject`)
15. **1h MACD histogram** gate (`histogram_1h_blocks_*_15m`)
16. BTC catalyst if `ltf_strength == 0` (`no_btc_catalyst_15m_unconfirmed`)
17. Build `est_prob_up`, edge, confidence (lag/spike boosts, corr damping, …)
18. **Macro leg floor (LONG only)** `macro_leg_blocks_long` — uses `_signal_lag_magnitude`; runs after edge build **by design** so cheap screens run first; veto means “catch-up thesis too weak for this YES-long”
19. AI marginal updown branch (optional)
20. `effective_min_edge`, `edge_below_min`, entry price band, `max_edge_updown`, sizing

## Optional single-candidate debug

At high log level, use the first skip `logger` line in the updown branch for a given market; `reason_parts` on an emitted signal lists macro, side, est_up, and mkt_yes.
