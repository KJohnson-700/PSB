---
date: 2026-08-02T13:00Z
type: psb-hourly-brief
hour: 1
focus: BTC (bitcoin strategy)
session: test_20260801_185754
format: cron-vault
---

# HOUR 1 — BTC (local bot, paper)

**Session:** test_20260801_185754 (started 2026-08-02T02:24:28Z, ~10h 35m elapsed)
**PID/liveness:** 20631 alive, 11h 03m uptime, RSS 784MB (hbeat) / 671MB (hbeat row), STAT=SN
**Cycle:** 663 cycles, cycle_elapsed_ms=2326, no overrun
**Per-strategy scan ms (last cycle):** bitcoin=51, bnb_macro=376, doge_macro=377, eth_macro=370, sol_macro=377, xrp_macro=546

## Headline — BTC is COMPLETELY STARVED

**Zero BTC entries in the entire ~10h 35m session.** Despite the strategy being enabled, BTC has been sitting in `signals=0`, `last_signal_counts.bitcoin=0`, `cumulative_signal_counts.bitcoin=1` — meaning even the single cumulative signal did not result in an entry. The bot is running BTC's scan loop (51ms per cycle) but is producing no fillable signals.

This is the "BTC is alive but not trading" pattern. The drift is real: **BTC was the only profitable lane on baseline (+$126.50, 53% WR on 1h; 5m/15m structural bleeders)** and is now producing zero entries despite being enabled. On baseline BTC 1h drove most of the profit; current session has 0 BTC entries → 0 BTC contribution to today's -$41.94 P&L.

## Why BTC is starved (skip-reason diagnostics, last pulse)

bot_runtime_status.json + OPS_JSON show the BTC strategy is choking on two skip reasons that have repeatedly blocked the only signals it's seen:

- **btc_15m_short_sitout**: 7 instances (skipping 15m SHORT candidates, likely the `allowed_side=SHORT` + HTF=BULLISH case where the strategy sits out)
- **btc_5m_short_quant_coinflip**: 2 instances (5m SHORT quant edge below `effective_min_edge=0.06` vs. `edge=0.006`)

The bias_is_BULLISH at HTF4 is forcing `allowed_side=SHORT` for all BTC signals in this regime, and the SHORT side is being filtered out by `btc_15m_short_sitout` + `btc_5m_short_quant_coinflip`. Net effect: BTC is permanently sidelined while the regime is BULLISH on the 4h.

**Top skip reasons across all strategies:** rsi_hard_blocked=17, btc_15m_short_sitout=7, lane_price_band=7, lane_entry_window=7, lane_min_edge=3, btc_5m_short_quant_coinflip=2.

## Lane posteriors (BTC, baseline-era data, last updated 2026-05-17 — STALE)

| Lane | alpha_ewma | n | Updated |
|---|---|---|---|
| bitcoin 15m down bearish drift | -0.701 | 21 | 2026-05-17 |
| bitcoin 15m down bearish predict_window | -1.775 | 22 | 2026-05-17 |
| bitcoin 15m down bearish standard | +1.486 | 38 | 2026-05-17 |
| bitcoin 5m down bearish drift | +0.128 | 35 | 2026-05-17 |
| bitcoin 5m down bearish predict_window | -2.225 | 3 | 2026-05-17 |
| bitcoin 5m down bearish standard | -0.439 | 45 | 2026-05-17 |
| bitcoin 5m down neutral drift | -2.136 | 8 | 2026-05-15 |
| bitcoin 5m down neutral spike | -0.349 | 6 | 2026-05-15 |
| bitcoin 1h down bearish standard | +2.602 | 1 | 2026-05-17 |

Note: all `last_updated` are 2026-05-17 or earlier — **BTC lane posteriors have not been updated since May 17** (~6 weeks stale). Cannot use them as current trading signal. Baseline SESSION-1H success (1h +$126.50, 53% WR) is not accessible here.

## Session-level P&L (not BTC)

- realized_pnl = **-$41.94** over 29 closed trades (20.7% WR, 6W/23L)
- loss_streak = 8 (HIGH — approaching alarm threshold)
- bankroll = $458.06, cash_bankroll = $458.06, open = 0
- worst_lane = eth|15m|down (-$9.67)
- worst bleeders this session: xrp_macro -$25.37 (17 trades, 23.5% WR), bnb_macro -$7.61 (0% WR), eth_macro -$7.19

BTC's contribution to -$41.94: **$0** (zero entries).

## Red flags

- **[WARN] BTC STARVED** — 0 entries in 10h35m, 1 cumulative signal, signals pulse=0. Should be a productive lane per baseline.
- **[WARN] loss_streak = 8** — exceeds the ≥5 alert threshold (sticky-bearish day, not BTC but session-wide).
- **[WARN] realized_pnl ≤ -$41.94** — exceeds the ≤-25 mid-day alarm.
- **[WARN] BTC lane_posteriors STALE 6+ weeks** — last updated 2026-05-17, cannot anchor decisions.
- **[INFO] Cycle scan timings healthy** — all per-strategy scans <1s, no cycle lag.
- **[INFO] RSS 784MB** — under 900MB ceiling, no swap concern.
- **[INFO] Bot alive, healthy** — cycle 663, clean phase lifecycle, no overrun.

## Drift callout

**BTC is the headline.** Zero BTC entries in 10h35m. The strategy is firing scan cycles but every SHORT-side signal is being filtered by `btc_15m_short_sitout` (7) and `btc_5m_short_quant_coinflip` (2) while HTF4=BULLISH dominates. The profitable 1h BTC lane from baseline is not present in this session at all. Session P&L is -$41.94 entirely from XRP/BNB/ETH macro bleed; BTC has had no opportunity to either win or lose.

## What this is NOT

- Not a wedge (cycle_elapsed_ms=2326, no overrun)
- Not a deadlock (signals pulse=0, but next-cycle phase=cycle_complete regularly)
- Not a feed/price-degen (updown_15m_count=36, updown_5m_count=10, updown_1h_count=19 — all healthy)
- Not AI-blocking (ai_decision_layer_skips=0 across all strategies)
- Not exposure-kill (loss_streak=8 hasn't tripped the consecutive-loss kill)

## Action

NONE. **Watch only.** Slim has been warned that BTC is now a no-op strategy. If unchanged at HOUR 2, it warrants a code review of BTC's bias-penalty / effective_min_edge logic under HTF4=BULLISH regimes.

## Sources

- data/runtime/bot_heartbeat.json (PID 20631, 13:00:30Z)
- data/runtime/bot_runtime_status.json (cycle 663, scan timings)
- data/logs/polybot_20260801.log (OPS_JSON last pulse 13:00:30Z)
- data/paper_trades/test_20260801_185754/entries.jsonl (60 lines, 29 trades closed)
- data/paper_trades/test_20260801_185754/summary.json
- data/calibration/lane_posteriors.json (BTC keys only)
- ~/.hermes/logs/psb_local_health.jsonl (last row 12:59:16Z)
