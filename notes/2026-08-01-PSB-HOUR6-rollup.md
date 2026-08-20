# PSB HOUR 6 — Daily Rollup (2026-08-01)

## TL;DR
- **Day PnL: -$82.58** across 3 sessions (57 closed, 17 wins, 29.8% WR)
- Current session `test_20260801_011345`: **-$23.73 realized, -$40.82 total** (15 closed, 6 wins, 40% WR)
- **Bot is alive** (PID 64210, RSS 909MB, heartbeat ~47s fresh) but **stalled on alt lanes**
- All 16 entries today are **bitcoin** only — sol/eth/hype/xrp/doge/bnb scanned extensively (160+ markets) but **0 entries**
- 2.5h flat PnL since last entry at 17:00 UTC; latest pulse (18:13 UTC) has 0 signals firing across all 7 lanes
- **Wedge (non-blocking):** health row reports `pid: null / rss_mb: null / priority: BOT_DEAD` despite heartbeat containing pid=64210 and rss_mb=909.4 — health writer cannot read heartbeat fields

## Daily Rollup by Session

| Session | Window | Closed | Wins | WR | Realized |
|---------|--------|--------|------|------|----------|
| test_20260731_161704 | 08-01 00:01–02:01 UTC | 14 | 5 | 35.7% | -$18.38 |
| test_20260731_190127 | 08-01 02:04–08:11 UTC | 28 | 6 | 21.4% | -$40.47 |
| test_20260801_011345 | 08-01 08:14–18:13 UTC (current) | 15 | 6 | 40.0% | -$23.73 |
| **TOTAL** | full day | **57** | **17** | **29.8%** | **-$82.58** |

## Current Session PnL Curve (hourly buckets)
- 09:00 UTC: 1 entry, 0 closed (-$0)
- 10:00 UTC: 3 entries, 1 closed (-$20.87)
- 11:00 UTC: 13 entries, 3 closed (-$34.28) — *peak loss territory*
- 12:00 UTC: 7 entries, 11 closed (-$44.07) — **intraday low**
- 13:00 UTC: 2 entries, 12 closed (-$34.81) — recovery begins
- 14:00 UTC: 4 entries, 13 closed (-$21.19) — best point
- 15:00 UTC: 0 entries, 14 closed (-$30.36)
- 17:00 UTC: 1 entry, 15 closed (-$23.73) — last entry
- 18:00 UTC: 0 entries, 15 closed (-$23.73) — **flat 2.5h**

## Per-Lane (current session)

| Lane | n | Wins | WR | Realized | alpha_ewma (v2) | Verdict |
|------|---|------|------|----------|------------------|---------|
| bitcoin|1h|up | 4 | 2 | 50% | -$14.84 | +0.006 (n=262) | weak-positive |
| bitcoin|5m|up | 9 | 3 | 33% | -$13.34 | -0.46 to +1.4 mix | **bleeder** |
| bitcoin|15m|down | 2 | 1 | 50% | +$4.45 | -0.71 (drift) | tiny sample |
| sol_macro | 0 | 0 | — | $0.00 | — | **DEAD** |
| eth_macro | 0 | 0 | — | $0.00 | — | **DEAD** |
| hype_macro | 0 | 0 | — | $0.00 | — | **DEAD** |
| xrp_macro | 0 | 0 | — | $0.00 | — | **DEAD** |
| doge_macro | 0 | 0 | — | $0.00 | — | **DEAD** |
| bnb_macro | 0 | 0 | — | $0.00 | — | **DEAD** |

## Last Hour Drift (all from OPS_JSON at 18:13 UTC)
- Bankroll: $476.27 cash, $459.18 equity, daily PnL -$23.73
- cycle_elapsed_ms: 2123ms (under 20s threshold)
- sync_phase_elapsed_ms: 1115ms
- updown_15m_count: 42 (healthy, >20)
- updown_5m_count: 10
- updown_1h_count: 26
- **allowed_side flipped to SHORT for: bitcoin, sol_macro, eth_macro, doge_macro** (all empty/no entries)
- **last_signal_counts = 0 across ALL 7 lanes** in the latest pulse
- btc_markets_considered: 16, sol_macro: 15, eth_macro: 12, hype_macro: 13, xrp_macro: 15, doge_macro: 13, bnb_macro: 12 (~80 markets scanned, 0 entries)
- Top skip reasons: lane_entry_window=34, buy_yes_15m_pocket_off=17, lane_min_edge=12, neutral_bias=8

## Red Flags

### [WARN] Bitcoin-only trading, alt lanes fully silenced
- 6 alt lanes scanned 80+ markets each with 0 entries
- Side selection forced SHORT but no SHORT signals are firing
- Likely: regime gate `chop` + allowed_side=SHORT + no qualifying edges → all BUY_NO flows blocked
- This is the *signature* of a bot that's "alive but not trading"

### [WARN] bitcoin|5m|up losing lane
- n=9, 33% WR, -$13.34
- Cumulative alpha on bullish|drift is +1.38 (n=112) but mixed across other v2 arms
- The 5m UP lane has been generating entries but converting poorly

### [WARN] 2.5h of no new entries
- Last entry 17:00 UTC, current 18:13 UTC
- Bot is scanning each minute but every cycle yields 0 signals
- Matches pattern: BULLISH regime upstream, but per-lane side gates forcing SHORT with no qualifying SHORT edges

### [INFO] Health row wedge (recurring signature)
- `pid: null, rss_mb: null, priority: BOT_DEAD, severity: dead` in health row
- Heartbeat.json itself contains `pid: 64210, rss_mb: 909.4`
- ps confirms PID 64210 alive, running 153:19 CPU
- Health writer is broken; process is fine. This is the same wedge from 07-28, 07-31

### [INFO] Cumulative scan volume
- xrp_macro: 53 markets scanned, 0 entries
- bitcoin: 27 cumulative signals but only 16 entries (lots of shadow decisions)
- 5 of 7 lanes have zero `last_signal_counts` in the last pulse

## Verdict
- **Day is a loss; current session is small win by comparison (40% WR vs 21% WR earlier)**
- Bot is alive but **stuck** — all 7 lanes returning 0 signals/cycle
- Bitcoin lane is the only productive lane; alt lanes are **dormant** despite active scanning
- Wedge is cosmetic (does not block trading); the underlying issue is regime+side-gate mismatch
- Not under -$25 threshold on a single-session basis, but **cumulative day -$82.58** is the bigger picture

## Baseline Comparison
- Reference: test_20260714_070245 = +$869.90 over 75h, 1138 entries, 46.4% WR
- Today: -$82.58 in 9h, 57 closed, 29.8% WR
- **WR is 16.6 pp below baseline**; everything is bleeding

## Files Touched
- None (read-only monitoring)
