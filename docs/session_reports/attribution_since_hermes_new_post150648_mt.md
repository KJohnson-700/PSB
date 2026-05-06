# Attribution since — paper journal

- **Label:** `hermes_new_post150648_mt`
- **Closed trades:** 39
- **Sessions included:** 7
- **Filters:** `{"since_iso": null, "from_first_line": "data/paper_trades/test_20260504_150648/entries.jsonl", "session_prefix": null, "after_mtime": "2026-05-04", "explicit_sessions": null}`

## Top loss buckets (strategy :: action :: exit_reason)

| strategy | action | exit_reason | n | wins | pnl | avg_pnl |
|---|---|---:|---:|---:|---:|---:|
| eth_macro | BUY_YES | updown_time_stop | 7 | 0 | -23.5750 | -3.3679 |
| hype_macro | BUY_YES | updown_time_stop | 5 | 0 | -13.3500 | -2.6700 |
| sol_macro | BUY_YES | updown_time_stop | 3 | 0 | -7.5250 | -2.5083 |
| hype_macro | BUY_YES | updown_expired | 1 | 0 | -4.5000 | -4.5000 |
| xrp_macro | BUY_YES | updown_time_stop | 1 | 0 | -4.2400 | -4.2400 |
| bitcoin | BUY_YES | updown_time_stop | 1 | 0 | -3.7000 | -3.7000 |
| xrp_macro | BUY_YES | RESOLVED:NO (real) | 1 | 0 | -2.8000 | -2.8000 |
| sol_macro | BUY_YES | RESOLVED:NO (real) | 1 | 0 | -2.3750 | -2.3750 |
| xrp_macro | BUY_YES | take_profit | 1 | 1 | 1.6250 | 1.6250 |
| hype_macro | BUY_YES | take_profit | 1 | 1 | 1.8500 | 1.8500 |
| bitcoin | BUY_YES | take_profit | 4 | 4 | 5.7000 | 1.4250 |
| eth_macro | BUY_YES | take_profit | 4 | 4 | 5.7000 | 1.4250 |
| sol_macro | BUY_YES | RESOLVED:YES (real) | 2 | 2 | 5.7000 | 2.8500 |
| sol_macro | BUY_YES | take_profit | 7 | 7 | 11.8500 | 1.6929 |

## ETH `eth_macro` — side_src × exit_reason

| side_src | exit_reason | n | pnl | avg_pnl |
|---|---|---:|---:|---:|
| signal_first_fallback | updown_time_stop | 7 | -23.5750 | -3.3679 |
| signal_first_fallback | take_profit | 4 | 5.7000 | 1.4250 |

## Exit stratification (Hermes buckets)

- **Overall:** `take_profit`=17, `updown_time_stop`=17, `RESOLVED:YES`=2, `RESOLVED:NO`=2, `updown_expired`=1, `other`=0; **tp_share** (TP / sum of non-other buckets)=0.4359


### By strategy

| strategy | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |
|---|---:|---:|---:|---:|---:|---:|---:|
| bitcoin | 4 | 1 | 0 | 0 | 0 | 0 | 0.8 |
| eth_macro | 4 | 7 | 0 | 0 | 0 | 0 | 0.3636 |
| hype_macro | 1 | 5 | 0 | 0 | 1 | 0 | 0.1429 |
| sol_macro | 7 | 3 | 2 | 1 | 0 | 0 | 0.5385 |
| xrp_macro | 1 | 1 | 0 | 1 | 0 | 0 | 0.3333 |

### By strategy × window_size

| strategy::window | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |
|---|---:|---:|---:|---:|---:|---:|---:|
| bitcoin::5m | 4 | 1 | 0 | 0 | 0 | 0 | 0.8 |
| eth_macro::15m | 1 | 0 | 0 | 0 | 0 | 0 | 1.0 |
| eth_macro::5m | 3 | 7 | 0 | 0 | 0 | 0 | 0.3 |
| hype_macro::15m | 1 | 1 | 0 | 0 | 0 | 0 | 0.5 |
| hype_macro::5m | 0 | 4 | 0 | 0 | 1 | 0 | 0.0 |
| sol_macro::5m | 7 | 3 | 2 | 1 | 0 | 0 | 0.5385 |
| xrp_macro::15m | 0 | 0 | 0 | 1 | 0 | 0 | 0.0 |
| xrp_macro::5m | 1 | 1 | 0 | 0 | 0 | 0 | 0.5 |

## Sessions

- `test_20260503_164708`
- `test_20260503_194556`
- `test_20260503_223335`
- `test_20260504_004812`
- `test_20260504_025753`
- `test_20260504_034719`
- `test_20260504_150648`