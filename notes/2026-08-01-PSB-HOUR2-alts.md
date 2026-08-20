# PSB HOUR 2 — alts brief (xrp / eth / sol)
**Local bot · session test_20260801_185754 · 2026-08-02 ~02:04Z**
**Brand-new session** (PID 20631, ~7m elapsed, cycle 6 of 60s). 0 entries, 0 closed, $0 PnL. Baseline 1138 entries/75h not reachable this hour. Lane posterior verdicts are based on the SHAPE of historical lane distribution — not a 20-trade read.

## Process health
- pid 20631 ALIVE, 17% CPU, 893MB RSS, elapsed 07:05
- heartbeat ts 2026-08-02T02:04:10Z, hb_age 37s, phase=cycle_complete, severity=warn
- bot_runtime_status: cycle_count=6, cycle_elapsed_ms=6585, scanner_sync_ms=2377, per-lane scan ~1.22s (×6 lanes, totaling strategy_scan_total_ms=1221 — that aggregate is suspect, see flag)
- bankroll $500.0, cash $500.0, equity $500.0
- AI on (2 providers, ready), WSS not in heartbeat but slug fetch all green (hit=attempted for 15m/5m/1h)
- exposure_loss_kill: enabled=true BUT apply_in_paper=false (correct, paper mode)

## Live lane status (current session, all 0 entries so far)
- **xrp_macro** — allowed_side=LONG, last pulse 13 BUY_YES attempts, 0 signals fired. Top skips: lane_entry_window=9, centered_price_edge_below_min=2, buy_yes_conviction_floor=1, late_window_blocked=1. Composite score 0.6138–0.6179 (below 0.68 BTC floor; xrp has no floor configured).
- **eth_macro** — allowed_side=null (NEUTRAL alt_1h_trend, no long allowed). 10 BUY_YES attempts, 0 signals. Top skips: lane_entry_window=7, neutral_bias=4, eth_5m_weak_confirm=2, nonpositive_edge=1.
- **sol_macro** — allowed_side=null (NEUTRAL), 7 BUY_YES, 0 signals. Top skips: lane_entry_window=6, neutral_bias=4, buy_yes_5m_disabled_lane=2, lane_min_edge=1.

## Lane posterior shape (calibration/lane_posteriors.json, non-v2 rows)
- **xrp_macro** (5 lanes, total_n=43): wtd α=+0.794. But MIXED — `xrp|15m|up|bullish|standard` α=-5.000 n=1, `xrp|15m|up|bullish|spike` α=-4.404 n=1 (small-sample noise). 15m down lanes are +3.638/-0.761. 5m down standard +0.996. **No calibrated loser (α<-0.5 with n>=50).**
- **eth_macro** (9 lanes, total_n=59): wtd α=+0.891. Strong mix: 5m down standard +1.992 n=30 (big positive contributor), 15m up bullish -2.783/-2.229 (small n=3+2). 5m down spike -3.500 n=4. **No calibrated loser.**
- **sol_macro** (4 lanes, total_n=41): wtd α=+2.441 (cleanest of the three). All lanes positive, biggest 5m down standard +2.238 n=29. **No calibrated loser.**

## Signal_reason pattern (latest pulse, xrp as example)
- xrp 15m window attempt: `xrp_15m_native` (6 occurrences), `xrp_1h_native` (5), `xrp_5m_native` (2). All LONG side. None of them are firing because (a) lane_entry_window filters 9/13, (b) centered_price_edge_below_min blocks 2 (edge < effective min, est_up 0.51-0.62 vs mkt 0.495-0.66), (c) buy_yes_conviction_floor kills 1.

## vs. baseline shape
Baseline test_20260714_070245 made xrp 37.5% of +$869 with mostly 15m native + LONG sources. Current pulse mirrors that source mix (xrp_15m_native dominant). But the current session is the EARLIEST phase — no trades have closed yet, so any "bleed" assessment is impossible at n=0.

## F6 fresh-cross LONG->SHORT gate (bitcoin live check)
- bitcoin lane: allowed_side=SHORT in current pulse. htf_bias=BEARISH. Last sample shows `htf_bullish_side_long` is NOT firing (gate enforcing short-only). The transition from baseline LONG-dominant → current SHORT-dominant is BTX bias swing, not the F6 gate. Need to check F6 is enforced not skipped — looking at the pulse: `bitcoin.action_counts={BUY_NO:1, BUY_YES:1}` so the bot IS attempting both sides; top_skip_reasons=lane_entry_window(8)+lane_price_band(2)+price_too_far_from_50_50(1)+buy_yes_conviction_floor(1). F6 is being respected.

## Red flag checklist
- [ ] BOT DEAD — no (pid 20631, RSS 894MB, heartbeat 37s)
- [ ] HEARTBEAT STALE — no (37s < 600s)
- [ ] CYCLE_LAG — no (cycle_elapsed 6.6s < 20s); per-lane scan 1.22s each is fine
- [ ] RSS > 900MB — no (894.5 < 900)
- [ ] session realized <= -25 — N/A (0 trades)
- [ ] LOSS_STREAK — N/A (0)
- [ ] LANE α<-0.5 with n>=50 — no (max n is eth 5m down standard n=30, all lanes are below 50)
- [ ] Pricing degenerate updown_15m_count < 20 — no (39 in heartbeat, 39 in ops_pulse)
- [ ] WSS disconnected — INFO [INFO]: WSS state not in heartbeat.json directly, but slug_fetch hit_rate=100% (63/63 15m, 14/14 5m, 35/35 1h, 4/4 hype_alt) so feed is live.

## Drift callout
**[INFO] xrp entry_lane is rejecting 9/13 attempts on lane_entry_window** — 5m/1h window windows are landing outside the 0.5-4 min entry zone. Not a red flag, but explains why 0 signals on a session with BULLISH bias.

## Verdict
Session is 7 minutes old, 0 trades, no realized PnL. Lane posteriors for xrp/eth/sol are NOT calibrated losers (weighted α all positive). Bot process healthy. Cannot assess bleed on n=0. No red flags.

## Top 3 red flags
**none** — all clear.
