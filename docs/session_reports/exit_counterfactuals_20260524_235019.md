## Lane Exit Counterfactual Analysis

**Entries:** `/Users/mainfolder/Documents/psb-main 1/data/paper_trades/test_20260524_060424/entries.jsonl`
**Eligible trades:** 200
**Triple-barrier params:** TP `0.50`, SL `0.20`
**Fetch missing OHLCV:** `True`

### Overall
- **Actual PnL:** +111.87 | **Hold PnL:** +117.71 | **Regret:** +5.84 | **Post-exit coverage:** 55.0%
- **Winner classes:** `{'good_capture': 22, 'gave_back_winner': 66, 'premature_take_profit': 17, 'insufficient_post_exit_path': 90, 'stop_saved_trade': 5}`

### Lane Table
| lane | trades | actual PnL | hold PnL | regret | med MFE | med MAE | capture | classes | recommendation |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| bitcoin|15m|down|bullish|drift | 16 | +6.58 | +12.40 | +5.81 | +5.53 | +4.94 | 0.52 | `{'premature_take_profit': 4, 'gave_back_winner': 9, 'good_capture': 3}` | test_trailing_exit_after_mfe |
| bitcoin|15m|down|bullish|predict_window | 12 | -14.62 | -33.78 | -19.16 | +3.62 | +7.50 | 0.64 | `{'gave_back_winner': 10, 'premature_take_profit': 1, 'good_capture': 1}` | test_trailing_exit_after_mfe |
| bitcoin|15m|down|bullish|spike | 3 | +12.34 | +24.87 | +12.53 | +12.47 | +2.47 | 0.79 | `{'good_capture': 2, 'gave_back_winner': 1}` | collect_more_samples |
| bitcoin|15m|down|bullish|standard | 6 | +2.28 | -11.53 | -13.81 | +5.92 | +5.00 | 0.93 | `{'gave_back_winner': 4, 'good_capture': 2}` | test_trailing_exit_after_mfe |
| bitcoin|1h|down|bullish|drift | 2 | +3.38 | +3.38 | +0.00 | +4.50 | +1.60 | 1.00 | `{'insufficient_post_exit_path': 2}` | collect_more_samples |
| bitcoin|1h|down|bullish|predict_window | 1 | -0.97 | -0.97 | +0.00 | +2.69 | +0.97 |  | `{'insufficient_post_exit_path': 1}` | collect_more_samples |
| bitcoin|1h|down|bullish|standard | 1 | -1.05 | -1.05 | +0.00 | +1.58 | +1.05 |  | `{'insufficient_post_exit_path': 1}` | collect_more_samples |
| bitcoin|5m|down|bullish|drift | 9 | +20.20 | +40.57 | +20.37 | +6.96 | +5.00 | 0.80 | `{'good_capture': 4, 'gave_back_winner': 4, 'premature_take_profit': 1}` | test_trailing_exit_after_mfe |
| bitcoin|5m|down|bullish|predict_window | 3 | -19.88 | -20.00 | -0.12 | +5.32 | +5.00 |  | `{'insufficient_post_exit_path': 2, 'gave_back_winner': 1}` | collect_more_samples |
| bitcoin|5m|down|bullish|spike | 8 | -6.89 | -15.94 | -9.05 | +2.06 | +7.50 | 0.40 | `{'premature_take_profit': 3, 'stop_saved_trade': 1, 'gave_back_winner': 4}` | test_trailing_exit_after_mfe |
| bitcoin|5m|down|bullish|standard | 19 | +6.35 | +12.72 | +6.37 | +4.20 | +5.00 | 0.71 | `{'good_capture': 3, 'gave_back_winner': 12, 'premature_take_profit': 2, 'insufficient_post_exit_path': 2}` | test_trailing_exit_after_mfe |
| bnb_macro|15m|down|bearish__bearish__bull|spike | 3 | +10.93 | +10.93 | +0.00 | +6.10 | +1.60 | 0.92 | `{'insufficient_post_exit_path': 3}` | collect_more_samples |
| bnb_macro|15m|down|bearish__bearish__bull|standard | 7 | -6.84 | -6.84 | +0.00 | +0.43 | +2.09 | 1.00 | `{'insufficient_post_exit_path': 7}` | keep_current_collect_more |
| bnb_macro|5m|down|bearish__bearish__bull|standard | 18 | -4.89 | -4.89 | +0.00 | +4.34 | +2.48 | 1.00 | `{'insufficient_post_exit_path': 18}` | keep_current_collect_more |
| doge_macro|15m|down|bearish__bearish__bull|spike | 3 | +1.20 | +1.20 | +0.00 | +1.05 | +2.70 | 1.00 | `{'insufficient_post_exit_path': 3}` | collect_more_samples |
| doge_macro|15m|down|bearish__bearish__bull|standard | 18 | +26.58 | +26.58 | +0.00 | +2.83 | +1.52 | 1.00 | `{'insufficient_post_exit_path': 18}` | keep_current_collect_more |
| doge_macro|1h|down|bearish__bearish__bull|standard | 1 | -6.19 | -6.19 | +0.00 | +0.00 | +6.19 |  | `{'insufficient_post_exit_path': 1}` | collect_more_samples |
| doge_macro|5m|down|bearish__bearish__bull|standard | 26 | -2.90 | -2.90 | +0.00 | +2.90 | +2.82 | 1.00 | `{'insufficient_post_exit_path': 26}` | keep_current_collect_more |
| eth_macro|5m|down|bearish__bearish__bull|standard | 3 | -20.84 | +6.25 | +27.09 | +7.84 | +7.66 |  | `{'gave_back_winner': 3}` | collect_more_samples |
| hype_macro|15m|down|bearish__bearish__bull|standard | 3 | -6.00 | -4.68 | +1.32 | +2.50 | +10.00 | 0.34 | `{'premature_take_profit': 1, 'stop_saved_trade': 1, 'gave_back_winner': 1}` | collect_more_samples |
| sol_macro|15m|down|bearish__bearish__bull|spike | 3 | +4.94 | +13.81 | +8.87 | +10.00 | +3.15 | 0.95 | `{'gave_back_winner': 1, 'good_capture': 1, 'stop_saved_trade': 1}` | collect_more_samples |
| sol_macro|15m|down|bearish__bearish__bull|standard | 3 | +4.54 | -1.31 | -5.85 | +2.29 | +5.00 | 0.97 | `{'good_capture': 1, 'gave_back_winner': 2}` | collect_more_samples |
| sol_macro|5m|down|bearish__bearish__bull|standard | 14 | +23.61 | -3.06 | -26.67 | +6.13 | +5.00 | 1.00 | `{'gave_back_winner': 7, 'insufficient_post_exit_path': 3, 'premature_take_profit': 1, 'good_capture': 2, 'stop_saved_trade': 1}` | test_trailing_exit_after_mfe |
| xrp_macro|15m|down|bearish__bearish__bull|spike | 3 | +15.31 | +19.21 | +3.90 | +11.74 | +0.56 | 0.65 | `{'premature_take_profit': 2, 'gave_back_winner': 1}` | collect_more_samples |
| xrp_macro|15m|down|bearish__bearish__bull|standard | 6 | +9.30 | +16.61 | +7.31 | +5.58 | +1.35 | 0.57 | `{'gave_back_winner': 3, 'good_capture': 1, 'premature_take_profit': 2}` | replay_higher_tp_or_trailing_profit |
| xrp_macro|5m|down|bearish__bearish__bull|standard | 9 | +55.39 | +42.31 | -13.08 | +8.33 | +10.00 | 1.00 | `{'insufficient_post_exit_path': 3, 'gave_back_winner': 3, 'good_capture': 2, 'stop_saved_trade': 1}` | test_trailing_exit_after_mfe |

### Notes
- **MFE/MAE** are measured in realized PnL dollars from the traded token path.
- **Hold PnL** uses cached OHLCV proxy settlement when available; otherwise it falls back to the final journal mark.
- **Do not change live exit settings** from this report alone unless lane sample size and post-exit coverage are adequate.
