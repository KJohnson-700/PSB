# Attribution since — paper journal

- **Label:** `gate_rebalance_baseline_old_034719`
- **Closed trades:** 115
- **Sessions included:** 1
- **Filters:** `{"since_iso": null, "from_first_line": null, "session_prefix": null, "after_mtime": null, "explicit_sessions": ["test_20260504_034719"]}`

## Top loss buckets (strategy :: action :: exit_reason)

| strategy | action | exit_reason | n | wins | pnl | avg_pnl |
|---|---|---:|---:|---:|---:|---:|
| bitcoin | BUY_YES | updown_time_stop | 13 | 0 | -41.9450 | -3.2265 |
| xrp_macro | BUY_YES | updown_time_stop | 6 | 0 | -18.9750 | -3.1625 |
| bitcoin | BUY_NO | updown_time_stop | 6 | 0 | -16.5500 | -2.7583 |
| sol_macro | BUY_YES | updown_time_stop | 4 | 0 | -11.0500 | -2.7625 |
| bitcoin | BUY_YES | RESOLVED:NO (real) | 2 | 0 | -9.4000 | -4.7000 |
| hype_macro | BUY_YES | updown_time_stop | 2 | 0 | -6.6000 | -3.3000 |
| bitcoin | BUY_NO | RESOLVED:YES (real) | 1 | 0 | -4.5750 | -4.5750 |
| sol_macro | BUY_NO | updown_time_stop | 1 | 0 | -3.7500 | -3.7500 |
| eth_macro | BUY_YES | updown_time_stop | 2 | 0 | -3.2000 | -1.6000 |
| xrp_macro | BUY_NO | updown_time_stop | 1 | 0 | -3.0500 | -3.0500 |
| sol_macro | BUY_NO | take_profit | 1 | 1 | 1.0500 | 1.0500 |
| hype_macro | BUY_YES | RESOLVED:YES (real) | 1 | 1 | 2.7750 | 2.7750 |
| bitcoin | BUY_NO | take_profit | 3 | 3 | 4.9000 | 1.6333 |
| sol_macro | BUY_YES | RESOLVED:YES (real) | 1 | 1 | 5.7000 | 5.7000 |
| eth_macro | BUY_YES | take_profit | 7 | 7 | 16.6500 | 2.3786 |
| hype_macro | BUY_YES | take_profit | 12 | 12 | 19.5500 | 1.6292 |
| sol_macro | BUY_YES | take_profit | 13 | 13 | 20.2500 | 1.5577 |
| xrp_macro | BUY_YES | take_profit | 16 | 16 | 24.4000 | 1.5250 |
| bitcoin | BUY_YES | take_profit | 23 | 23 | 46.1500 | 2.0065 |

## ETH `eth_macro` — side_src × exit_reason

| side_src | exit_reason | n | pnl | avg_pnl |
|---|---|---:|---:|---:|
| signal_first_fallback | updown_time_stop | 2 | -3.2000 | -1.6000 |
| signal_first_fallback | take_profit | 7 | 16.6500 | 2.3786 |

## BTC counter-trend subset (`bitcoin`)

- n=9, wins=2, pnl=-17.025, win_rate=0.2222


## Exit stratification (Hermes buckets)

- **Overall:** `take_profit`=75, `updown_time_stop`=35, `RESOLVED:YES`=3, `RESOLVED:NO`=2, `updown_expired`=0, `other`=0; **tp_share** (TP / sum of non-other buckets)=0.6522


### By strategy

| strategy | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |
|---|---:|---:|---:|---:|---:|---:|---:|
| bitcoin | 26 | 19 | 1 | 2 | 0 | 0 | 0.5417 |
| eth_macro | 7 | 2 | 0 | 0 | 0 | 0 | 0.7778 |
| hype_macro | 12 | 2 | 1 | 0 | 0 | 0 | 0.8 |
| sol_macro | 14 | 5 | 1 | 0 | 0 | 0 | 0.7 |
| xrp_macro | 16 | 7 | 0 | 0 | 0 | 0 | 0.6957 |

### By strategy × window_size

| strategy::window | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |
|---|---:|---:|---:|---:|---:|---:|---:|
| bitcoin::15m | 10 | 4 | 0 | 1 | 0 | 0 | 0.6667 |
| bitcoin::5m | 16 | 15 | 1 | 1 | 0 | 0 | 0.4848 |
| eth_macro::15m | 1 | 0 | 0 | 0 | 0 | 0 | 1.0 |
| eth_macro::5m | 6 | 2 | 0 | 0 | 0 | 0 | 0.75 |
| hype_macro::15m | 6 | 0 | 0 | 0 | 0 | 0 | 1.0 |
| hype_macro::5m | 6 | 2 | 1 | 0 | 0 | 0 | 0.6667 |
| sol_macro::5m | 14 | 5 | 1 | 0 | 0 | 0 | 0.7 |
| xrp_macro::15m | 10 | 2 | 0 | 0 | 0 | 0 | 0.8333 |
| xrp_macro::5m | 6 | 5 | 0 | 0 | 0 | 0 | 0.5455 |

## Sessions

- `test_20260504_034719`