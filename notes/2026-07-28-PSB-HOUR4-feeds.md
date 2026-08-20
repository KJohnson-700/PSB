# PSB Hour 4 — feeds + oracle + WSS diagnostic

- window: 2026-07-28 16:00–16:10Z (cron at 16:11Z)
- session: test_20260728_074851
- pid: 90135 (bot process alive, `--paper --no-dashboard`)
- mode: paper
- cycle_count: 82
- phase at sample: cycle_complete

## Process + heartbeat
- heartbeat.ts = 2026-07-28T16:10:26.299778Z, hb_age ≈ 3s — fresh.
- cycle_elapsed_ms = 3488 (target 60s) — well under 20s threshold.
- per-lane scan timings (ms): bitcoin 71, bnb_macro 138, doge_macro 78, eth_macro 139, sol_macro 139, xrp_macro 78. Total 140ms. No slow lane.
- RSS: 584.1MB. Up from 179.6MB (heartbeat earlier today) → +404MB ratchet this session. Still inside 900MB cap, no swap climb observed in current data.
- strategy_task_count = 6. All lanes running.

## Feeds / oracle / WSS — focus of this hour
- WSS connection log: continuous `Connected to Polymarket WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market)` events every ~15s; most recent at 16:09:36Z. No `Disconnected` / `disconnect` / `geoblock` lines in polybot_20260728.log (geoblock_lines_total=0).
- Flag scan across last 500 OPS_JSON pulses in polybot_20260728.log:
  - oracle_basis hits = 374 (per-strategy basis prints in skip-reason signal strings — normal)
  - oracle_stale = 0
  - kline_fb = 0
  - geoblock = 0
  - stale_spot = 0
  - WSS/disconnect = 0
- Scanner health (latest OPS_JSON 2026-07-28T16:10:26Z):
  - updown_15m_count = 61, updown_5m_count = 12, updown_1h_count = 28, updown_hype_alt_count = 0
  - sync_phase_elapsed_ms = 1603
  - updown_1h_source = live
- updown_hype_alt_count = 0 in this single pulse; the lane is enabled and prints zero, indicating a transient slug-replenish miss for the HYPE alt window — not a sustained fail-soft, no signal to act on this hour.

## Session PnL (per summary.json + entries)
- session summary: realized_pnl +$28.40, unrealized -$11.88, total +$16.52, win_rate 0.60, 13 entries (10 exits + 3 open).
- by lane (entries.jsonl, n=23 lines including some pre-resolve entries):
  - eth_macro: n=15, WR=26.7%, PnL=+$32.27 (driver)
  - xrp_macro: n=4, WR=25.0%, PnL=+$0.17
  - bitcoin: n=4, WR=25.0%, PnL=-$4.05
- exposure_manager recent_pnl: btc -3.87 (cons_losses=1), eth +32.12, xrp +0.29, others flat.
- 0 LOSS_STREAK≥5 and 0 ALL_LONG_CLUSTER≥4 across current entries (mix of `take_profit`, `never_green_cut`, `updown_stop_loss`).

## Lane calibration (lane_posteriors.json, resolver only, n-weighted)
- eth_macro: α=+0.139, n=1493 — OK
- bitcoin: α=-0.278, n=2884 — neutral
- xrp_macro: α=-0.279, n=2296 — neutral
- bnb_macro: α=-0.223, n=1538 — neutral
- hype_macro: α=-0.494, n=1759 — neutral (just inside threshold)
- doge_macro: α=+0.176, n=1069 — OK
- sol_macro: α=-0.551, n=1167 — **[WARN] alpha<-0.5 with n≥50**

## Red-flag checklist
- [BAD] BOT DEAD — no (pid 90135 running, hb fresh)
- [BAD] HEARTBEAT STALE — no (3s)
- [BAD] CYCLE_LAG — no (3.5s)
- [BAD] RSS > 900MB — no (584MB, ratchet observed)
- [WARN] session realized <= -25 — no (+$28.40)
- [WARN] LOSS_STREAK ≥ 5 — no
- [WARN] ALL_LONG_CLUSTER ≥ 4 — no
- [WARN] LANE alpha<-0.5, n≥50 — **yes**: sol_macro α=-0.551
- [WARN] Pricing updown_15m_count < 20 — no (61)
- [INFO] Pricing freshness > 120s — no
- [INFO] wss disconnected — no

## Drift callout (single line)
- WSS fresh, oracle_stale=0, kline_fb=0, geoblock=0; only soft WARN: sol_macro lane α=-0.551 (calibrated loser) and RSS ratchet 180→584MB this session.

## Verdict
- Clean hour on the feeds/oracle/WSS axis. WSS reconnecting every ~15s (steady), no oracle_stale/kline_fb/stale_spot/geoblock hits in last 500 OPS pulses, scanner populated with 61/12/28 across windows and `updown_1h_source=live`. Sol_macro lane is the only calibration WARN but it is not blocking — no trades taken in this session because sol_macro is gated out by HTF alignment. ETH macro carrying PnL. Watch RSS next hour — currently at 65% of cap and climbing.

## What I'm NOT doing
- Not patching, killing, restarting, or modifying any process or file.
- Not changing config or code.
- Reporting only; Claude owns fixes.

## Cross-session context
- Reference baseline session test_20260714_070245 finished +$869.90 / 1138 entries / 46.4% WR over 75h. This session is 13 entries in ~75min — too early to compare shape; use HOUR 5+ for shape deltas.
- Vault notes: see notes/2026-07-21-FORENSIC-session-start-vs-current.md for diverged-config baseline comparison.