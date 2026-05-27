## May 22 Regression Audit

**Generated:** `2026-05-26T20:56:26.701237+00:00`
**Baseline rule:** GOLD baseline = top 2 sessions by realized PnL on 5/22 with closed trades >= 50; ties by trade count then session id. (ref=HEAD)
**Current rule:** CURRENT = newest 2 sessions by session id with closed trades >= 50, excluding selected baseline sessions. (ref=HEAD)
**Baseline sessions:** `test_20260522_052210, test_20260522_020412`
**Current sessions:** `test_20260526_042005, test_20260525_231430`
**Closed trades:** baseline `330`, current `314`

### Section A — Session Table
| role | session | n | hrs | WR | PnL | $/trade | avg size | YES / NO |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | test_20260522_020412 | 130 | 3.24 | 36.9% | $36.06 | $0.28 | $20.94 | 1 / 129 |
| baseline | test_20260522_052210 | 200 | 6.52 | 42.5% | $292.27 | $1.46 | $21.94 | 4 / 196 |
| candidate | test_20260522_171336 | 74 | 2.53 | 36.5% | $-66.17 | $-0.89 | $24.61 | 0 / 74 |
| current | test_20260525_231430 | 145 | 4.47 | 41.4% | $0.34 | $0.00 | $22.18 | 3 / 142 |
| current | test_20260526_042005 | 169 | 9.36 | 35.5% | $-43.28 | $-0.26 | $20.47 | 12 / 157 |

### Section B — Baseline Selection

- **GOLD rule:** GOLD baseline = top 2 sessions by realized PnL on 5/22 with closed trades >= 50; ties by trade count then session id. (ref=HEAD)
- **Selected GOLD sessions:** `test_20260522_052210, test_20260522_020412`
- **Current comparison rule:** CURRENT = newest 2 sessions by session id with closed trades >= 50, excluding selected baseline sessions. (ref=HEAD)
- **Selected current sessions:** `test_20260526_042005, test_20260525_231430`

### Section C — Per-Strategy Economics
| strategy | base n | base WR | base avg win | base avg loss | base W/L | base size | now n | now WR | now avg win | now avg loss | now W/L | now size | W/L delta | size delta | |W/L| >20% |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bitcoin | 128 | 35.9% | $8.21 | $-3.58 | 2.29 | $22.96 | 100 | 36.0% | $7.54 | $-4.16 | 1.81 | $22.73 | -21.0% | -1.0% | yes |
| doge_macro | 32 | 25.0% | $8.25 | $-3.56 | 2.32 | $17.56 | 56 | 42.9% | $6.59 | $-5.87 | 1.12 | $21.97 | -51.6% | 25.1% | yes |
| sol_macro | 44 | 43.2% | $7.66 | $-3.50 | 2.19 | $20.76 | 44 | 34.1% | $5.60 | $-3.28 | 1.71 | $19.77 | -21.9% | -4.8% | yes |
| bnb_macro | 39 | 59.0% | $6.60 | $-5.18 | 1.27 | $22.06 | 41 | 41.5% | $7.27 | $-4.45 | 1.63 | $20.60 | 28.1% | -6.6% | yes |
| xrp_macro | 43 | 51.2% | $8.26 | $-3.40 | 2.43 | $23.19 | 28 | 32.1% | $4.56 | $-3.43 | 1.33 | $18.21 | -45.2% | -21.4% | yes |
| hype_macro | 15 | 26.7% | $8.68 | $-2.28 | 3.81 | $17.88 | 24 | 41.7% | $6.52 | $-4.37 | 1.49 | $21.66 | -60.9% | 21.2% | yes |
| eth_macro | 29 | 37.9% | $6.26 | $-2.89 | 2.17 | $19.66 | 21 | 42.9% | $5.98 | $-4.77 | 1.25 | $20.40 | -42.2% | 3.8% | yes |

### Direction Mix
| strategy::action | base n | base WR | base PnL | now n | now WR | now PnL |
| --- | --- | --- | --- | --- | --- | --- |
| bitcoin::BUY_NO | 128 | 35.9% | $83.78 | 94 | 36.2% | $5.17 |
| doge_macro::BUY_NO | 32 | 25.0% | $-19.35 | 56 | 42.9% | $-29.83 |
| sol_macro::BUY_NO | 44 | 43.2% | $58.05 | 44 | 34.1% | $-11.02 |
| bnb_macro::BUY_NO | 39 | 59.0% | $69.02 | 41 | 41.5% | $16.74 |
| xrp_macro::BUY_NO | 43 | 51.2% | $110.27 | 28 | 32.1% | $-24.16 |
| hype_macro::BUY_NO | 15 | 26.7% | $9.63 | 24 | 41.7% | $3.90 |
| eth_macro::BUY_NO | 24 | 37.5% | $16.03 | 12 | 41.7% | $-8.06 |
| eth_macro::BUY_YES | 5 | 40.0% | $0.89 | 9 | 44.4% | $4.64 |
| bitcoin::BUY_YES | 0 | na | $0.00 | 6 | 33.3% | $-0.32 |

### Section D — Per-Side-Source
| side_source | base n | base WR | base $/trade | now n | now WR | now $/trade | $ impact |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bearish_dip_default | 138 | 45.7% | $1.43 | 182 | 39.0% | $-0.29 | $314.20 |
| btc_htf_bias | 128 | 35.9% | $0.65 | 99 | 36.4% | $0.08 | $56.70 |
| alt_1h_legacy_btc_mode | 29 | 37.9% | $0.58 | 21 | 42.9% | $-0.16 | $15.67 |
| bearish_dip_exception | 35 | 37.1% | $0.85 | 9 | 44.4% | $1.32 | $-4.31 |
| neutral_macro | 0 | na | na | 2 | 0.0% | $-1.63 | $3.26 |
| btc_quant_disagree_flip | 0 | na | na | 1 | 0.0% | $-3.25 | $3.25 |

### Section E — Per-Exit-Reason
| exit reason | base n | base total | base avg | base avg win | base avg loss | now n | now total | now avg | now avg win | now avg loss |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| updown_stop_loss | 151 | $-502.65 | $-3.33 | na | $-3.33 | 135 | $-538.18 | $-3.99 | na | $-3.99 |
| take_profit | 110 | $685.94 | $6.24 | $6.24 | na | 109 | $642.26 | $5.89 | $5.89 | na |
| stop_loss | 40 | $-168.25 | $-4.21 | na | $-4.21 | 54 | $-274.89 | $-5.09 | na | $-5.09 |
| RESOLVED:NO (real) | 23 | $340.42 | $14.80 | $14.80 | na | 10 | $143.39 | $14.34 | $14.34 | na |
| RESOLVED:YES (real) | 3 | $-25.00 | $-8.33 | na | $-8.33 | 4 | $-13.49 | $-3.37 | $11.51 | $-8.33 |
| updown_time_stop | 3 | $-2.14 | $-0.71 | na | $-0.71 | 2 | $-2.01 | $-1.01 | na | $-1.01 |

### Per-Lane WR / Selection
| lane | base n | base WR | base avg | base W/L | now n | now WR | now avg | now W/L |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| eth_macro|5m|down|bearish__bearish__bull|alt_1h_legacy_btc_mode | 0 | na | na | na | 5 | 20.0% | $-5.39 | 0.47 |
| xrp_macro|15m|down|bearish__bearish__bull|spike | 4 | 75.0% | $6.19 | 2.83 | 5 | 60.0% | $1.09 | 1.58 |
| eth_macro|15m|up|bullish__bullish__bull|standard | 3 | 66.7% | $2.00 | 1.07 | 1 | 0.0% | $-2.83 | na |
| doge_macro|15m|down|bearish__bearish__bull|spike | 2 | 50.0% | $2.22 | 3.32 | 5 | 20.0% | $-2.23 | 0.94 |
| xrp_macro|5m|down|bearish__neutral__bull|bearish_dip_default | 0 | na | na | na | 5 | 0.0% | $-3.98 | na |
| sol_macro|5m|down|bearish__neutral__bull|bearish_dip_default | 0 | na | na | na | 3 | 0.0% | $-3.41 | na |
| bitcoin|5m|down|bearish|drift | 11 | 27.3% | $3.11 | 5.56 | 0 | na | na | na |
| xrp_macro|5m|down|bearish__bearish__bull|standard | 18 | 55.6% | $3.10 | 2.00 | 0 | na | na | na |
| eth_macro|5m|down|bearish__bearish__bull|drift | 4 | 50.0% | $2.57 | 2.34 | 0 | na | na | na |
| bitcoin|5m|down|neutral|predict_window | 4 | 50.0% | $2.42 | 1.78 | 0 | na | na | na |
| bitcoin|1h|down|bearish|drift | 6 | 50.0% | $2.27 | 2.85 | 0 | na | na | na |
| bitcoin|1h|down|bearish|predict_window | 4 | 50.0% | $2.22 | 4.09 | 0 | na | na | na |
| xrp_macro|15m|down|bearish__bearish__bull|standard | 20 | 40.0% | $1.22 | 2.69 | 9 | 22.2% | $-0.90 | 2.24 |
| bitcoin|5m|down|bearish|standard | 22 | 50.0% | $1.93 | 2.19 | 0 | na | na | na |
| doge_macro|5m|down|bearish__bearish__bull|bearish_dip_default | 0 | na | na | na | 28 | 32.1% | $-1.92 | 1.18 |
| bnb_macro|15m|down|bearish__bearish__bull|spike | 4 | 25.0% | $-1.64 | 1.66 | 2 | 0.0% | $-3.48 | na |
| bnb_macro|5m|down|bearish__bearish__bull|standard | 19 | 63.2% | $1.74 | 1.03 | 0 | na | na | na |
| bitcoin|5m|down|neutral|drift | 14 | 35.7% | $1.58 | 2.91 | 0 | na | na | na |
| sol_macro|5m|down|bearish__bearish__bull|bearish_dip_default | 0 | na | na | na | 14 | 21.4% | $-1.44 | 2.09 |
| sol_macro|5m|down|bearish__bearish__bull|standard | 25 | 40.0% | $1.36 | 2.28 | 0 | na | na | na |

### Section F — Hypothesis Ledger
| strategy | $ impact | classification | standing | base n | now n | size Δ | W/L Δ | WR pp Δ | selection evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| xrp_macro | $95.96 | sizing+exit+selection | partly_explained_by_known_sizing_revert | 43 | 28 | -21.4% | -45.2% | -19.02 | xrp_macro|15m|down|bearish__bearish__bull|standard; xrp_macro|5m|down|bearish__neutral__bull|bearish_dip_default |
| sol_macro | $69.07 | exit+selection | claim_still_standing | 44 | 44 | -4.8% | -21.9% | -9.09 | sol_macro|5m|down|bearish__bearish__bull|bearish_dip_default |
| bitcoin | $60.60 | exit+selection | claim_still_standing | 128 | 100 | -1.0% | -21.0% | 0.06 | bitcoin|15m|down|bearish|htf_bearish_side_short |
| bnb_macro | $55.82 | exit | claim_still_standing | 39 | 41 | -6.6% | 28.1% | -17.51 |  |
| eth_macro | $15.67 | exit+selection | claim_still_standing | 29 | 21 | 3.8% | -42.2% | 4.93 | eth_macro|5m|down|bearish__bearish__bull|alt_1h_legacy_btc_mode |
| hype_macro | $11.51 | sizing+exit | partly_explained_by_known_sizing_revert | 15 | 24 | 21.2% | -60.9% | 15.00 |  |
| doge_macro | $-4.03 | sizing+exit+selection | partly_explained_by_known_sizing_revert | 32 | 56 | 25.1% | -51.6% | 17.86 | doge_macro|5m|down|bearish__bearish__bull|bearish_dip_default |

### Interpretation Guardrails

- This report uses closed ENTRY/EXIT pairs from `entries.jsonl` and applies the repo phantom-exit filter.
- Ghost logs cannot validate exit/stop/sizing regressions; this report uses actual journal exits for those economics.
- Groups with fewer than 15 trades are directional evidence, not proof.
- Hypothesis classifications are independent flags: sizing = avg size moved >=10%; exit = W/L ratio moved >=20%; selection = lane WR deterioration or new low-WR current lane.

### Metadata/Summary

Tags: #PSB #RegressionAudit #May22Baseline #TradeJournal
Related Concepts: [[May 22 Baseline]], [[Exit Economics]], [[Kelly Sizing]], [[Lane Attribution]]
Summary: This audit compares the May 22 baseline sessions against current paper sessions using closed journal trades. It isolates whether degradation is coming from strategy economics, direction mix, exit reasons, or lane-level admission.
