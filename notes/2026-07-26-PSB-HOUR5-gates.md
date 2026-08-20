# PSB HOUR 5 — Lane gates + calibration (LOCAL bot)

**Date:** 2026-07-26T02:13Z · **Session:** test_20260725_185351 · **PID:** 48147 (alive 19m25s) · **Cycle:** 18 · **RSS:** 1009MB

## Reconciliation
- Cron prompt cited `polybot_20260720.log` for OPS_JSON. That file is stale (last entry 2026-07-21T00:46:32Z — *5 days ago*; different session test_20260720_181702). Current active session is `test_20260725_185351` with bankroll=$513.86, 83 cumulative entries.
- Session in cron prompt example (PID 82410) is dead. Current PID 48147 confirmed via `ps -p` (etime=19:25).
- Latest OPS_JSON read is from the stale 20260720 log; bankroll/closed_trades diverged between the two sessions — do NOT compare across sessions, only intra-session shape. The health row and entries are from the **live** session.

## Health row (most recent)
- ts=2026-07-26T02:10:06Z, hb_age=16s, phase=cycle_complete, rss_mb=875.1
- severity=**crit**, priority=**PNL_CRIT**
- realized=**-$43.16**, closed=3, wins=0, losses=3, WR=0%
- loss_streak=**3**, long_loss_cluster=1, exp_long_losses=1
- worst_lane=`sol|1h|up` @ -$2.88
- bankroll_drift=$9.54 · cash_bankroll=$466.37 · deployed=$23.13 · open=2
- giveback_usd=$8.32 · tpl_fires=0 · doge5mdn_fills=0
- lanes (session): xrp|5m|up n=1 w=0 pnl=-$38.90, eth|1h|down n=1 w=0 pnl=-$1.38, sol|1h|up n=1 w=0 pnl=-$2.88

## Live OPS_JSON (stale 20260720 log, 2026-07-21T00:46Z — pre-dates current session)
- bankroll=$513.86, equity=$513.10, open=6, closed=77, total_entries=83, daily_trades=83
- realized=**-$29.36**, total_pnl=-$30.12, daily_pnl=+$13.86
- cumulative_signal_counts: eth_macro=806 (heaviest), xrp_macro=67, sol_macro=53, hype=11, doge=6, btc=5, bnb=5
- All 7 lanes returned signals=0 this pulse — gates neutral_bias=8 (hype) + lane_min_edge=12 dominate skip mass
- allowed_side aggregate: LONG=5, SHORT=2 (sol/hype)
- Recent 121-pulse side_rollup: bitcoin LONG=120/120, bnb 114L/6S, sol 74L/46S, eth 107L/10S, xrp 100L/20S, doge 98L/20S, hype 102L/18S
- AI: live, 2 chains, ready — calls=5 (sol=3, eth=1, xrp=1)
- Pricing: updown_15m=**61** (>20 healthy), 5m=12, 1h=29, hype_alt=0
- Sync phase elapsed=1597ms (healthy)

## Cycle timings (cycle_elapsed_ms=6084 of 60000 budget)
- strategy_scan_total_ms=1298 · scanner_sync_ms=3196 · resolution_check=1037
- Per-lane: bnb_macro 1295 (slowest, 1.3s — under 1s budget slightly exceeded on bnb only — borderline), bitcoin 130, doge 152, eth 146, sol 154, xrp 153
- cycle_overrun_ms=0 — no overrun flag

## Exposure managers (current)
| key | tier | mult | consec_losses | portfolio_pnl |
|---|---|---|---|---|
| bnb | MODERATE | 0.6 | 0 | -$33.63 |
| btc | MODERATE | 0.6 | 0 | -$33.63 |
| doge | MODERATE | 0.6 | 0 | -$33.63 |
| eth | MODERATE | 0.6 | 1 | -$33.63 |
| hype | MODERATE | 0.6 | 0 | -$33.63 |
| sol | MODERATE | 0.6 | 1 | -$33.63 |
| xrp | MODERATE | 0.6 | 1 | -$33.63 |

All 7 at MODERATE; eth/sol/xrp have 1 consec_loss; none paused. exposure_loss_kill_enabled=true (threshold=3).

## Session entries (test_20260725_185351) — fresh, 5 entries / 3 exits in 15 min
- xrp_macro BUY_YES @0.55 5m → EXIT updown_stop_loss @0.02, **-$38.90** (mae -74.6%, hold 136s, slip=0.25)
- eth_macro BUY_NO @0.52 1h → EXIT updown_stop_loss @0.43, **-$1.38** (mae -16.4%, hold 270s)
- sol_macro BUY_YES @0.52 1h → EXIT updown_stop_loss @0.42, **-$2.88** (mae -24%, hold 43s)
- btc BUY_YES @0.57 1h (opened 01:01) — still open
- eth_macro BUY_NO @0.51 (opened 01:57) — still open

Total session PnL: -$44.47 (realized -$43.17 + unrealized -$1.30). WR=0% on 3 completed.

## Per-lane WR / PnL (this session, n=5 — too low for veto stats)
| strategy | trades | wins | WR | PnL |
|---|---|---|---|---|
| xrp_macro | 1 | 0 | 0% | -$38.90 |
| eth_macro | 1 | 0 | 0% | -$1.38 |
| sol_macro | 1 | 0 | 0% | -$2.88 |
| bitcoin | 1 | 0 | open | open |
| bitcoin (open) | 1 | — | — | -$1.30 unreal |

n=1 per lane → **insufficient for wr_veto_threshold (0.4) recalibration**. recompute_on_settle=FROZEN=false is unchanged; vetos cannot fire on n<50 sample.

## Lane_posteriors alpha_ewma — bleeders with n ≥ 50 (calibrated losers, session-irrelevant)
| variant | n | alpha | beta WR |
|---|---|---|---|
| `hype_macro 5m up bullish__bullish__bull hype_5m_native` | 352 | -1.614 | 0.360 (89W/158L) |
| `bitcoin 15m up bullish drift` | 356 | -0.514 | 0.374 |
| `bitcoin 1h up bullish htf_bullish_side_long` | 248 | -1.369 | 0.433 |
| `sol_macro 5m down bearish__bearish__bear sol_5m_native` | 168 | -1.339 | 0.436 |
| `bnb_macro 5m up bullish__bullish__bull bnb_5m_native` | 150 | -0.695 | 0.386 |
| `bitcoin 15m up bullish spike` | 147 | -0.893 | 0.469 |
| `xrp_macro 1h down bearish__bearish__bear xrp_1h_native` | 117 | -0.543 | 0.508 |
| `bnb_macro 5m down bullish__bullish__bull bnb_5m_native` | 91 | -0.625 | 0.430 |
| `bnb_macro 5m down bearish__bearish__bear bnb_5m_native` | 90 | -0.894 | 0.382 |
| `bitcoin 5m down neutral htf_neutral_side_short` | 54 | -1.023 | 0.453 |
| `doge_macro 15m down bearish__neutral__bull doge_15m_native` | 50 | -0.884 | 0.563 |
| `hype_macro 15m up bullish__bullish__bear hype_15m_native` | 59 | -1.389 | 0.344 |
| `hype_macro 5m down bearish__bearish__bull spike` | 9 | -1.153 | 0.429 (n<50) |

Per strategy aggregate (v2_source_resolver, n-weighted from previous HOUR-5 vault + spot check):
- hype_macro α≈-0.505 (n=1756, WR=0.420) — calibrated loser
- bnb_macro α≈-0.357 (n=1500, WR=0.442)
- sol_macro α≈-0.810 (n=1054, WR=0.455) — calibrated loser
- bitcoin α≈-0.096 (n=2789, WR=0.429)
- doge_macro α≈-0.043 (n=983, WR=0.460)
- xrp_macro α≈+0.191 (n=2088, WR=0.478)
- eth_macro α≈+0.231 (n=1328, WR=0.422)

**None currently meet WR<40% with both n≥50 AND alpha<-0.5 simultaneously for this bot's overall summary** (only specific variants inside each lane do).

## Red flag checklist
- [OK] Bot alive (PID 48147, 19m25s uptime)
- [OK] Heartbeat fresh (hb_age=16s, ts=02:13:02Z; phase=cycle_start on latest hb)
- [OK] cycle_elapsed_ms=6084 (well under 60s)
- [WARN] **bnb_macro scan 1295ms** (>1s SLA — flagged, not critical)
- [WARN] **RSS 1009MB** (>900MB ceiling) — bot 19min old, will likely stabilize; 100MB above the soft ceiling
- [WARN] **session realized=-$43.17** on a 19-min session, WR=0% on n=3 (small sample but all three exits were updown_stop_loss with 14-74% MAE)
- [WARN] **xrp|5m|up -$38.90 single stop-out** at -74.6% MAE with book spread 0.25 (liquidity gap pattern, exit_book_spread=0.25 vs typical <0.05)
- [INFO] All 7 exposure managers at MODERATE tier; eth/sol/xrp at 1 consec_loss (kill threshold=3); not paused.
- [INFO] recompute_on_settle FROZEN=false per operator 07-13 — vetos still won't auto-fire on lanes with WR drift and large n (operator's choice).
- [INFO] OPS_JSON bankroll=$513 vs summary cash_bankroll=$466 → $47 gap is the entry cost basis (deployed=$23 + open unrealized). No accounting_error.

## Verdict
**Live session is 19 min old and down $43 on 3 stop-outs.** The single largest loss is a **xrp|5m|up LONG that stopped at -74.6% MAE with exit_book_spread=0.25** — a liquidity-void stop-out, not a directional thesis failure. The lane_posterior aggregate holds: hype_macro and sol_macro are still the calibrated losers (α<-0.5 with n≥100); bitcoin/bnb show alpha drift in specific variants but not strategy-wide. Session WR=0% on n=3 is too small to invalidate lane priors. Watch the next 30 min — if xrp stops out 2 more times, the bot's exposure manager (kill at 3 consec) will pause xrp.

## Top drift callouts (for Claude if flagged)
1. **Cron log path is stale** — prompt points at `polybot_20260720.log` which is from 2026-07-21. Live log for THIS session is `polybot_20260725.log`. Cron silently reads a 5-day-old file.
2. **xrp|5m|up stop-out at 0.25 exit_book_spread** is the worst mark for this strategy in recent memory. If repeated, the `effective_stop_loss_pct=0.5` is being applied to a thin book — consider tightening xrp_5m_native stop.
3. **bnb_macro scan 1.3s** is borderline — only lane over 1s budget. Acceptable for now.
