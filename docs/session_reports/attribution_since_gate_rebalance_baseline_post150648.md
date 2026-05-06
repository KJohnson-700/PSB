# Attribution since — paper journal

- **Label:** `gate_rebalance_baseline_post150648`
- **Closed trades:** 66
- **Sessions included:** 10
- **Filters:** `{"since_iso": null, "from_first_line": "data/paper_trades/test_20260504_150648/entries.jsonl", "session_prefix": null, "after_mtime": "2026-05-04", "explicit_sessions": null}`

## Top loss buckets (strategy :: action :: exit_reason)

| strategy | action | exit_reason | n | wins | pnl | avg_pnl |
|---|---|---:|---:|---:|---:|---:|
| eth_macro | BUY_YES | updown_time_stop | 7 | 0 | -23.5750 | -3.3679 |
| hype_macro | BUY_YES | updown_time_stop | 6 | 0 | -15.1500 | -2.5250 |
| sol_macro | BUY_YES | updown_time_stop | 6 | 0 | -14.8750 | -2.4792 |
| bitcoin | BUY_YES | updown_time_stop | 6 | 0 | -14.8500 | -2.4750 |
| hype_macro | BUY_YES | updown_expired | 1 | 0 | -4.5000 | -4.5000 |
| xrp_macro | BUY_YES | updown_time_stop | 1 | 0 | -4.2400 | -4.2400 |
| xrp_macro | BUY_YES | RESOLVED:NO (real) | 1 | 0 | -2.8000 | -2.8000 |
| sol_macro | BUY_YES | RESOLVED:NO (real) | 1 | 0 | -2.3750 | -2.3750 |
| xrp_macro | BUY_YES | take_profit | 3 | 3 | 4.5750 | 1.5250 |
| hype_macro | BUY_YES | take_profit | 4 | 4 | 5.4500 | 1.3625 |
| eth_macro | BUY_YES | take_profit | 4 | 4 | 5.7000 | 1.4250 |
| sol_macro | BUY_YES | RESOLVED:YES (real) | 2 | 2 | 5.7000 | 2.8500 |
| hype_macro | BUY_YES | RESOLVED:YES (real) | 1 | 1 | 5.8000 | 5.8000 |
| bitcoin | BUY_YES | take_profit | 11 | 11 | 16.8000 | 1.5273 |
| sol_macro | BUY_YES | take_profit | 12 | 12 | 18.9750 | 1.5813 |

## ETH `eth_macro` — side_src × exit_reason

| side_src | exit_reason | n | pnl | avg_pnl |
|---|---|---:|---:|---:|
| signal_first_fallback | updown_time_stop | 7 | -23.5750 | -3.3679 |
| signal_first_fallback | take_profit | 4 | 5.7000 | 1.4250 |

## Exit stratification (Hermes buckets)

- **Overall:** `take_profit`=34, `updown_time_stop`=26, `RESOLVED:YES`=3, `RESOLVED:NO`=2, `updown_expired`=1, `other`=0; **tp_share** (TP / sum of non-other buckets)=0.5152


### By strategy

| strategy | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |
|---|---:|---:|---:|---:|---:|---:|---:|
| bitcoin | 11 | 6 | 0 | 0 | 0 | 0 | 0.6471 |
| eth_macro | 4 | 7 | 0 | 0 | 0 | 0 | 0.3636 |
| hype_macro | 4 | 6 | 1 | 0 | 1 | 0 | 0.3333 |
| sol_macro | 12 | 6 | 2 | 1 | 0 | 0 | 0.5714 |
| xrp_macro | 3 | 1 | 0 | 1 | 0 | 0 | 0.6 |

### By strategy × window_size

| strategy::window | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |
|---|---:|---:|---:|---:|---:|---:|---:|
| bitcoin::15m | 3 | 1 | 0 | 0 | 0 | 0 | 0.75 |
| bitcoin::5m | 8 | 5 | 0 | 0 | 0 | 0 | 0.6154 |
| eth_macro::15m | 1 | 0 | 0 | 0 | 0 | 0 | 1.0 |
| eth_macro::5m | 3 | 7 | 0 | 0 | 0 | 0 | 0.3 |
| hype_macro::15m | 4 | 1 | 0 | 0 | 0 | 0 | 0.8 |
| hype_macro::5m | 0 | 5 | 1 | 0 | 1 | 0 | 0.0 |
| sol_macro::5m | 12 | 6 | 2 | 1 | 0 | 0 | 0.5714 |
| xrp_macro::15m | 1 | 0 | 0 | 1 | 0 | 0 | 0.5 |
| xrp_macro::5m | 2 | 1 | 0 | 0 | 0 | 0 | 0.6667 |

## Sessions

- `test_20260503_164708`
- `test_20260503_194556`
- `test_20260503_223335`
- `test_20260504_004812`
- `test_20260504_025753`
- `test_20260504_034719`
- `test_20260504_150648`
- `test_20260504_195754`
- `test_20260504_220335`
- `test_20260504_220539`