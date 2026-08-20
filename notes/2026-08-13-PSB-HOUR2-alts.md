# PSB HOUR 2 — alts brief (xrp / eth / sol)
**Local bot · session test_20260813_052734 · 2026-08-13 ~15:06Z**
**Brand-new session** (PID 72706, ~35m elapsed since 14:31:12Z, cycle_count=316 by etime but only 2 closed trades). Cold-start tier per run-63/77. Realized sample is tiny (n=2 closes) — verdicts are forward-looking from v2 lane posteriors + recent rollup.

## Process health
- pid 72706 ALIVE, hb_age 5s, phase=cycle_complete, severity=ok, priority=ok
- heartbeat.json content ts 15:05:49Z (fresh in-memory) BUT mtime 08:06:15Z (7h stale on disk) → **phantom-writer content-fresh variant**, RSS gap = heartbeat 781.6MB vs healthrow 714.9MB = 66.7MB (mild band per run-77)
- bot_runtime_status: cycle_count=316, cycle_elapsed_ms=7259, scanner_sync_ms=2822, strategy_scan_total_ms=2513
- per-lane scan: bitcoin 39ms (fast outlier), alts bnb/doge/eth/sol/xrp ~930ms each (shared caller bottleneck, ~930ms per lane = group driver), hype_macro 2511ms (single driver — matches total). 23s margin to 30s cycle = healthy.
- bankroll $985.65, cash $985.65, equity $985.65, realized -$14.35
- cumulative_signal_counts (121-pulse lookback): bitcoin=11, sol_macro=0, eth_macro=2, hype_macro=33, xrp_macro=8, doge_macro=10, bnb_macro=12
- updown_15m_count=47 (healthy), updown_5m_count=13 (soft-warn — 5m alpha view degraded)
- AI on (2 providers, ready), shadow_pipeline firing on sol/eth/xrp/doge/bnb (5/5 calls, 0 ok = shadow only)

## Realized PnL (session)
- 2 entries, 2 closed, 0 open, 0W-2L = 0% WR
- Both xrp_macro 5m, both catastrophic_stop (never_green_stop)
  - 14:31Z xrp 5m|down BUY_NO, -$8.34, mae_pct -49.0%, hold 17s, lane_id xrp_macro|5m|down|bearish__bearish__bull|xrp_5m_native_market_favorite (lane α updated to +5.0 n=1 — only 1 sample)
  - 14:46Z xrp 5m|up BUY_YES, -$6.01, mae_pct -48.0%, hold 41s, lane_id xrp_macro|5m|up|bullish__neutral__bull|standard (lane α updated to -5.0 n=1)
- eth_macro & sol_macro: ZERO entries this session
- exposure_managers.xrp.consecutive_losses=1 (max_consecutive_losses=3 → 2 away from pause)

## Lane posterior shape (v2 lanes, n>=50 filter)
**ALTS_LONG_FLOOR_BLEEDING (full): 6 v2 LONG-direction buckets α<-0.5 ∧ n>=50**

| Strategy | Window | Side | Source | n | α | Last updated |
|---|---|---|---|---|---|---|
| eth_macro | 15m | up | eth_15m_native | 90 | −1.324 | 2026-08-12T12:52Z |
| eth_macro | 1h | up | eth_1h_native | 60 | −2.729 | 2026-08-10T09:25Z |
| eth_macro | 5m | up | eth_5m_native | 54 | −1.833 | 2026-08-10T02:11Z |
| sol_macro | 15m | up | sol_15m_native | 131 | −0.903 | 2026-08-05T18:32Z |
| xrp_macro | 1h | up | xrp_1h_native | 100 | −1.120 | 2026-08-09T22:32Z |
| xrp_macro | 5m | up | xrp_5m_native | 502 | −1.793 | 2026-08-04T19:38Z |

Per recipe run-61 ladder: 6 of 7+ (run-61 actual: 5/7) = full ALTS_LONG_FLOOR_BLEEDING. The xrp_macro 5m|up cohort is the highest-traffic alts bleeder (n=502, α=-1.793), confirming baseline reference — xrp 5m|up is the calibrated side losing on long sample. **The session's first BUY_YES (5m|up standard) entered at lane_id `xrp_macro|5m|up|bullish__neutral__bull|standard` with α=-5.0 n=1 FRESH — this lane was previously untouched in v2 (no prior n≥50 sample for the bullish__neutral__bull regime).** Lane now being seeded under cold-start regime.

Two session exits JUST updated the lane_posteriors at 14:31:29Z and 14:46:56Z — these are n=1 snapshots, not stable calibration yet.

## Side rollup (121-pulse lookback)
- bitcoin: 92L / 22S = 80.7% LONG
- xrp_macro: 31L / 59S = 65.6% SHORT — **SHORT bias on xrp over the lookback window**
- eth_macro: 51L / 43S = 54.3% LONG
- sol_macro: 64L / 48S = 57.1% LONG
- doge_macro: 44L / 44S = 50/50
- bnb_macro: 49L / 17S = 74.2% LONG
- aggregate.LONG=4 (bitcoin, sol, doge, bnb), SHORT=2 (eth, hype), unknown=1 (xrp allowed_side=null)

**Not** an ALL_DOWN_CLUSTER fingerprint (LONG ≥ SHORT overall). xrp is the lone SHORT-biased alt (consistent with prior-session bleed cleanup pattern — the 121-pulse window spans the prior dying session which may have been SHORT-dominant on xrp before bleeding).

## Regime context
- primary_htf_bias=NEUTRAL (clean neutral), alt_htf_bias=NEUTRAL, btc_htf_bias=NEUTRAL
- btc_htf_vote_details: bull_votes=2 (price_vs_ma BULL, macd BULL), bear_votes=1 (sabre BEAR), bias=NEUTRAL (mixed)
- last_cycle per-strategy all 15:05:49Z (scan alive)

## F6 fresh-cross LONG→SHORT gate
- Working-tree was live in bitcoin/sol/eth per the cron hint. Current pulse: bitcoin allowed_side=LONG, sol allowed_side=LONG, eth allowed_side=SHORT. No F6 flip observed this pulse. The two xrp closes both landed at catastrophic_stop within seconds (17s, 41s) — gate fires before the price has time to make the move.
- The lane_id for the 14:46Z trade = `bullish__neutral__bull` regime (PRIMARY_ALT_HTF=BULLISH at entry time, ltf=NEUTRAL, btc=NEUTRAL). Session had this set to BULLISH at entry even though current pulse shows NEUTRAL — that's the run-78 open-position-past-market-end reverse: position-management gap in the regime-authorship history.

## vs. baseline shape
Baseline test_20260714_070245 made xrp 37.5% of +$869 ($326), eth_macro 10.4% ($90), sol_macro 8.4% ($76). Current session: only xrp is filling (n=2, both L), eth/sol silent. **Cold-start signature**: a fresh 35-min session where the gates haven't yet routed eth/sol to fills. The xrp_5m_native_market_favorite lane (run-78 standard) IS firing — both session losses landed on this lane.

## Red flag checklist
- [ ] BOT DEAD — no (pid 72706, hb_age 5s)
- [ ] HEARTBEAT STALE — no (5s < 600s); **[INFO] phantom-writer persistent content-fresh variant: 7h since last disk-flush, RSS gap 66.7MB (mild band)**
- [ ] CYCLE_LAG — no (cycle_elapsed 7.3s < 20s); per-lane ~930ms = shared caller bottleneck (group driver, run-78 baseline band 1.0-2.0s = healthy)
- [ ] RSS > 900MB — no (healthrow 714.9, heartbeat 781.6; phantom-writer divergence in band)
- [ ] session realized <= -25 — no ($-14.35, n=2 cold-start)
- [ ] lane <= -15 — no (single xrp lane $-14.35 under cold-start sample)
- [ ] LOSS_STREAK — yes: 2 (xrp consecutive), under threshold 5
- [ ] ALL_LONG_CLUSTER — no (aggregate 4L/2S/1u, not all-LONG)
- [ ] LANE α<-0.5 with n>=50 — **yes: 6 v2 LONG-direction buckets in xrp/eth/sol macros — [WARN] ALTS_LONG_FLOOR_BLEEDING (full, 6/7)**
- [ ] Pricing degenerate updown_15m_count < 20 — no (47 healthy)
- [ ] Pricing soft-warn updown_5m_count < 15 — yes (13 → 5m alpha view degraded)
- [ ] Drift callout — **mandatory, 22nd recurrence** (prompt still hardcodes polybot_20260720.log, canonical is data/logs/ops_pulse.jsonl)

## Forward-looking notes
1. The two xrp closes are n=1 lane_posterior updates (lane α just seeded with one catastrophic_stop). Calibration will converge as more trades land. **Cold-start verdict: defer to HOUR 3** — only 35 min of session data, too thin for stable per-strategy verdicts.
2. eth/sol silence under NEUTRAL primary_htf is by-design (run-74 pattern: lanes without allowed_side don't fire). Once regime resolves, expect fills to resume.
3. ALTS_LONG_FLOOR_BLEEDING (full, 6/7) is a session-level calibration fingerprint, not a session bleeding signal. The 1h/15m/5m LONG-direction cohorts are calibrated losers on long sample. Forward-looking: if NEUTRAL → BULLISH flips, the LONG scaffold will be the routing destination and the calibrated cohorts will fill.
4. xrp consecutive_losses=1 (threshold 3) → next xrp close at L would put it AT the kill threshold (paper ignores, but live would pause).
5. The 14:46 BUY_YES entry has lane_id `bullish__neutral__bull|standard` but primary_htf_bias at entry was `BULLISH` (now NEUTRAL). Run-78 reverse fingerprint: position-management didn't re-evaluate after bias flipped. The position closed 41s later at -49% MAE so the gap is moot for this trade, but the lane_id regime is now stale.

## Bottom line
Cold-start session (35m), only xrp filled (2 catastrophic_stops, $-14.35). Eth/sol silent. v2 calibration flags 6/7 LONG-direction buckets as calibrated bleeders across alts (full ALTS_LONG_FLOOR_BLEEDING). Watch next 1-2h for eth/sol routing to resume and xrp to cross the kill threshold (consecutive_losses=1 of 3). No [BAD] flags.
