# PSB HOUR 2 — alts

- Checked at 2026-07-22 04:02 UTC; local session `test_20260721_165755`, PID 55355.
- Bot is alive: heartbeat age 14s, cycle_complete, RSS 703.8MB. No cycle lag/overrun surfaced; pricing count 62.
- Session realized PnL is -$45.11, crossing the -$25 warning threshold. Worst lane is sol|1h|up at -$27.33 (4 trades, 25% WR).
- Recent entries (last 20 available per strategy): xrp 12 entries, 0 wins, -$32.59 realized; eth 20, 7 wins, +$0.23; sol 19, 5 wins, -$24.99. Entry records include open rows with pnl 0, so these are available-row summaries rather than closed-only WR.
- Current OPS pulse is stale/mismatched: timestamp 2026-07-21 07:46 UTC and session `test_20260720_181702`, while health is current session. Do not use it as current-session proof; it does show xrp LONG, eth LONG, sol SHORT and xrp/sol signal-reason gates.
- Calibration warnings: xrp has alpha_ewma -2.627 (n=70) on 5m up bearish/bear xrp_5m_vs_slower, -1.317 (n=81) on 1h up bullish/bull xrp_1h_native, and -0.651 (n=379) on 5m up bullish/bull xrp_5m_native. Sol has -2.628 (n=118) on 5m down bearish/bear sol_5m_native and -1.014 (n=66) on 15m down bearish/bear sol_15m_native. Eth’s n>=50 negative-alpha bucket is only -0.266 (n=59), below the -0.5 alert threshold.
- F6 fresh-cross LONG→SHORT gate: no direct current-session trade reason was available in the sampled entries; stale OPS pulse shows sol’s window_delta_flip→BUY_NO sample was rejected for lane_min_edge, while xrp was LONG-only in that pulse.
