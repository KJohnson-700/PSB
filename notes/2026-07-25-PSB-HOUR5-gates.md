# PSB HOUR 5 — Lane gates + calibration (LOCAL bot)

**Date:** 2026-07-25T01:20Z · **Session:** test_20260722_200518 · **PID:** 44611 (alive 1d22h) · **Cycle:** 2490 · **RSS:** 765MB

## Reconciliation note
Cron prompt referenced stale `data/logs/polybot_20260720.log` (file mtime 2026-07-21 00:47 — dead 4 days). Active log is `data/logs/polybot_20260722.log` (1.35M lines, last entry 2026-07-25T01:19Z). The bot has been on `test_20260722_200518` since 2026-07-23T03:05Z. PID 82410 cited in the prompt is dead — current PID is 44611.

## Health row (most recent)
- ts=2026-07-25T01:16:15Z, hb_age=18s, phase=cycle_complete, rss=804.2MB
- severity=**crit**, priority=**LANE_BLEED**
- realized=$443.68, closed=598, wins=284, losses=314, WR=47.5%
- loss_streak=2, long_loss_cluster=0
- worst_lane=`doge|5m|down` @ -$56.33 (n=36, 13W)
- bankroll_drift=$269.24 (cash_bankroll=$1212.81 vs $943.57 base)
- giveback_usd=$1195.38 (cumulative realized giveback)
- tpl_fires=29, tpl_pnl=$177.75

## Live OPS_JSON (2026-07-25T01:19:04Z, polybot_20260722.log)
- bankroll=$1212.81, equity=$1212.81, open=0, closed=598
- BTC flipped **BEARISH** (spot $64,018, htf_bias=BEARISH, 4H MACD=-217, mome dead)
- All alts now BEARISH or NEUTRAL (was BULLISH during first ~30h)
- Cumulative signals: eth_macro=529, sol_macro=499, xrp_macro=436, bitcoin=221, doge=163, bnb=72, hype=13
- **All 7 lanes returned signals=0 this pulse** — gates neutral_bias + lane_min_edge saturating
- aggregate neutral_bias skips=25 (top reason), lane_entry_window=19, lane_min_edge=11, tape_arbitration_stale_side_chop=11
- AI: live, 2 chains, ready, calls=1 (sol shadow)
- Pricing: updown_15m=48, 5m=12, 1h=24, hype_alt=0 (all > 0, healthy)

## Cycle timings (cycle_elapsed_ms=14581, cycle_interval=60000)
- strategy_scan_total_ms=2559 · scanner_sync_ms=3577
- Per-lane: bitcoin 155, bnb/doge/eth/sol/xrp 907-913, **hype_macro 2557** (slowest)
- cycle_overrun_ms=0 → no overrun flag, but elapsed is 14.6s out of 60s — healthy margin

## Exposure managers (current)
| key | tier | mult | consec_losses | recent_pnl | recent_trades |
|---|---|---|---|---|---|
| bnb | MINIMAL | 0.2 | 1 | +$36.92 | 43 |
| btc | MINIMAL | 0.2 | 1 | +$10.15 | 50 |
| doge | MINIMAL | 0.2 | 0 | -$6.42 | 50 |
| eth | MINIMAL | 0.2 | 2 | +$0.59 | 50 |
| sol | MODERATE | 0.6 | 0 | -$17.63 | 50 |
| hype | MODERATE | 0.6 | 1 | -$3.92 | 8 |
| xrp | MODERATE | 0.6 | 0 | +$26.11 | 50 |

All 7 at MINIMAL/MODERATE — no PAUSED. No exposure kill triggered.

## Per-lane session stats (from summary.json)
| strategy | trades | wins | WR | PnL | avg |
|---|---|---|---|---|---|
| eth_macro | 146 | 60 | 41.1% | +$41.00 | +$0.28 |
| xrp_macro | 100 | 52 | 52.0% | +$61.05 | +$0.61 |
| sol_macro | 115 | 57 | 49.6% | +$137.52 | +$1.20 |
| bnb_macro | 42 | 25 | 59.5% | +$129.64 | +$3.09 |
| bitcoin | 120 | 62 | 51.7% | +$147.44 | +$1.23 |
| doge_macro | 67 | 26 | 38.8% | -$45.47 | -$0.68 |
| hype_macro | 8 | 2 | **25.0%** | -$27.60 | -$3.45 |

**Bleeders (WR < 40%):** hype_macro (25%, 8t, -$27.60), doge_macro (38.8%, 67t, -$45.47). Both well below wr_veto_threshold but **recompute_on_settle is FROZEN=false** per operator 07-13 — vetos are NOT auto-applied. This is the expected drift the cron prompt asks about.

## Lane_posteriors alpha_ewma aggregate (v2_source_resolver, n-weighted)
| strategy | n | alpha_avg | post_WR (beta) |
|---|---|---|---|
| bitcoin | 2789 | -0.096 | 0.429 |
| bnb_macro | 1500 | -0.357 | 0.442 |
| doge_macro | 983 | -0.043 | 0.460 |
| eth_macro | 1328 | +0.231 | 0.422 |
| hype_macro | 1756 | **-0.505** | 0.420 |
| sol_macro | 1054 | **-0.810** | 0.455 |
| xrp_macro | 2088 | +0.191 | 0.478 |

## Bleeders (n ≥ 30, alpha < -0.5) — top variants
| variant | n | alpha |
|---|---|---|
| `hype_macro 5m up bullish__bullish__bear hype_5m_native` | 352 | -1.614 |
| `bitcoin 1h up bullish htf_bullish_side_long` | 248 | -1.369 |
| `sol_macro 5m down bearish__bearish__bear sol_5m_native` | 166 | -0.681 |
| `bnb_macro 5m up bullish__bullish__bull bnb_5m_native` | 150 | -0.695 |
| `bitcoin 15m up bullish spike` | 147 | -0.893 |
| `bitcoin 15m up bullish drift` | 356 | -0.514 |
| `bnb_macro 5m down bearish__bearish__bear bnb_5m_native` | 90 | -0.894 |
| `xrp_macro 5m down bullish__bullish__bull xrp_5m_native_window_delta_flip` | 32 | -1.858 |
| `doge_macro 1h up bullish__bullish__bull standard` | 61 | -3.344 |
| `hype_macro 5m up bearish__bearish__bear standard` | 45 | -3.130 |
| `bitcoin 5m down bearish htf_bearish_side_short` | 74 | -2.921 |

## Red flag checklist
- [OK] Bot alive (PID 44611, 1d22h uptime)
- [OK] Heartbeat fresh (hb_age=18s, ts=01:19:04Z vs 01:20:36Z)
- [OK] cycle_elapsed_ms=14581 (well under 60s budget)
- [OK] RSS 765MB (under 900MB ceiling)
- [OK] realized=$443.57 session (+$16.89 daily)
- [WARN] **HYPE_MACRO lane bleeding**: 25% session WR on 8 trades (low n), alpha=-0.505, alpha=-3.13 on 5m-up-bearish-bear. cumulative_signal_count=13 (low engagement). Treated as expected drift — operator 07-13 froze recompute_on_settle.
- [WARN] **DOGE_MACRO lane bleeding**: 38.8% WR on 67 trades, alpha=-3.34 on 1h-up-bullish. Cumulative signals=163. PnL=-$45.47.
- [WARN] **frozen recompute_on_settle**: per the operator note, beta-veto cannot fire on hype/doge WR drift even though criteria met (wr_veto_threshold=0.4).
- [INFO] BTC regime flipped BEARISH 2026-07-25; all alts flipped too. allowed_side collapsed to `null` for xrp/doge/bnb (NEUTRAL). Allowed LONG-only lanes: bitcoin. SHORT-only: sol/eth/hype. Neutral lanes: xrp/doge/bnb.
- [INFO] updown_15m_count=48 (decent; was 61 earlier). No API slow flag.

## Verdict
**Session shape: +$443.57 over 2 days at 47.5% WR.** Bleeders are concentrated in **hype_macro** (block-trade low-n signal amplifier) and **doge_macro** (pocket-off pattern still producing). The frozen recompute_on_settle is the operator's deliberate choice — these lanes are not being auto-vetoed. As long as global session PnL stays positive and single-lane drawdown stays < -$60, drift is expected.

## Top drift callouts (for Claude if flagged)
1. **cron's hardcoded log path is stale** — `data/logs/polybot_20260720.log` is from 2026-07-21 00:47. Active log is `polybot_20260722.log`. Cron prompt will silently read 4-day-old JSON if the prompt repeats. Not urgent but worth fixing the path token.
2. **hype_macro alpha drifts toward -3.13 on 5m-up-bearish-bear** — this is the worst variant in the system. If/when recompute_on_settle unfreezes, expect immediate veto recommendation.
3. **doge_macro pocket-off skips=9** for 5m lane = 75% of doge-5m-up signals being filtered. Threshold tuning might increase fire rate.
