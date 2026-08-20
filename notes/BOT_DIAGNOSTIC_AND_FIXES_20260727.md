# PSB BOT — FULL DIAGNOSTIC & FIX SPRINT (2026-07-27, pre-10pm PT)

Goal: overview everything learned about this bot's Polymarket trading, enumerate every issue still
hindering it, research fixes (Claude + Codex + 3 code deep-dives), implement the safe high-value ones
(backups + Codex review + py_compile), paper-test, stage for the strong 10pm→11am window.

Status: investigations running (Codex + agents: exit-anchor bug / scanner+oracle / legacy force-side).
This file = the running synthesis.

---

## PART A — WHAT THIS BOT'S POLYMARKET TRADING ACTUALLY IS (learned)

**Instrument.** Binary up/down markets per asset (BTC/ETH/SOL/XRP/HYPE/DOGE/BNB), windows 5m / 15m / 1h.
Each resolves YES (up) or NO (down) → $1 / $0. `BUY_YES`=long/up, `BUY_NO`=short/down. Entry price =
token price ∈ (0,1). Win pays `size×(1−entry)`, loss = `−size×entry`.

**Edge = selection, not calibration.** est_prob_up is ~0.50 AUC (barely better than a coin) — the edge
comes from SELECTING which candidates to take, not from a precise probability. Absolute edge:
BUY_YES = est_up − yes_price; BUY_NO = yes_price − est_up.

**The 0.50 trap.** Entries at yes_price ≈ 0.50 with thin edge (~0.05) are coin flips. This bot's worst
losses are midline entries — especially where a legacy band FORCES a side there (see B3). Winners are
CHEAP entries (0.17/0.23) where the alt-native read strongly disagrees with the market and resolves in
favor → 4–5× payout (e.g. xrp 5m NO +$73 on a $15 stake this session).

**Execution (direct-CLOB).** FAK/FOK = marketable (take the book now); GTC = resting limit that can sit
unfilled and let a position ride to resolution. Loss-cutting exits MUST be FAK. The CLOB order-status
pipeline is unreliable (filled/MATCHED orders can read as PENDING) → needs a venue reconciler
(Data-API /positions) as backstop.

**Exits are where edge leaks.** The ENTRY edge is real; gap-through stops, ride-to-zero (GTC rests),
and — this session — SUPPRESSED exits (bad held-time anchor) all bleed the entry edge back out.

**Alts are alt-native.** SOL/XRP/DOGE/HYPE/BNB decided by their own indicators, not BTC. sol_macro is
the base class; xrp/hype/bnb/doge are thin subclasses. bitcoin.py and eth_macro.py are standalone
(duplicate the scan loop) — fixes to sol_macro do NOT auto-port to them.

**Collection lanes.** eth 5m/15m win by taking profit BEFORE resolution — resolution-WR understates
them. Don't judge/cut them on resolution WR alone.

**Timing (Pacific).** Best 10pm→11am, peak 5–7am (+$177–253/hr); worst 5–9pm (−$65 to −$88/hr).
Trade the good hours; standing down in the red block is a valid "recovery" lever.

---

## PART B — CONFIRMED PROBLEM INVENTORY (this session)

| # | Sev | Problem | Status |
|---|-----|---------|--------|
| B1 | 🔴CRIT | **Exit-suppression anchor bug** — resumed positions compute `held-eff ≈ −7.8h` ("window-open anchored") < min-hold → suppresses never_green_cut/stops/TP → losers ride to FULL resolution loss. Root of today's bleed. | agent tracing |
| B2 | 🔴CRIT | **CLOB status-misreport** — get_order_status returns PENDING for orders MATCHED+MINED on-venue → stuck "still pending" loops + phantom count. Reconciler backstops after 3 snapshots. | staged reconciler; needs status fix |
| B3 | 🟠HIGH | **btc 1h simple_long forces midline longs** at 0.50–0.54 bypassing min_edge → −$33 this session. | ✅ FIXED (entry_min 0.55 staged, Codex GO) |
| B4 | 🟠HIGH | **CLOB market-order entry 400 "invalid amounts"** (maker/taker precision swapped) → 76-min entry outage. | ✅ STAGED (create_market_order) |
| B5 | 🟡MED | **eth 5m/15m BUY_NO 0.50 coin-flips** — chronic marginal loss; no absolute-edge / distance-from-0.50 floor. | needs entry brake |
| B6 | 🟡MED | **Tape adapter declawed** — tape_freshness size-only (max_edge_add 0.0); lane_tape_adapter inert (empty root config, all deltas 0.0). Not the recovery lever (blind to fresh coin flips) but noted. | analysis done |
| B7 | 🟡MED | **oracle_basis_block starving entries** — basis −32..−36 bps vs max 18–25 blocks most candidates. Real arb vs stale oracle? | agent tracing |
| B8 | 🟡MED | **Sizing oversizes low-edge coin flips** — true-Kelly on miscalibrated est_prob. Calibration-correction hook exists but shadow shows it'd cut collection lanes (net −). | needs edge-proportional approach |
| B9 | 🟢FIXED | **Dashboard P&L trace scale bug** — paper-init 500 snapshot blew the y-axis. | ✅ FIXED (scale-guard, Codex GO) |
| B10 | 🟡MED | **Concentration risk** — all 6 assets went SHORT simultaneously (correlated). Portfolio exposure cap? | agent tracing |

---

## PART C — DEEP-DIVE FINDINGS (Codex + agents)  [PENDING — fill on return]

### C1. Exit-anchor bug — CONFIRMED (agent + Codex agree)
Root: live_testing.py:721 `held_eff = hours_held*3600 − _preopen_lag_secs`; :320 lag=`max(0,(end−opened)−wl)`. Algebra → `held_eff = now − (end_date − window_len) = now − window_open`. Resumed positions carry a serialized `end_date` (main.py:1906, never re-validated) whose derived window_open is in the FUTURE → held_eff ≈ −7.8h → line 726 `held_eff < min_hold` suppresses stop/TP/never_green → `reason=None` (line 750) → losers ride to resolution. Operator already annotated it at main.py:3901.
- **🔥 HOT kill-switch (no restart):** `trading.exit_rules.updown_min_hold_anchor_window_open: false` → `_preopen_lag_secs` returns 0 (line 301) → held_eff = true entry age (always +). reload_from_config (live_testing.py:141, called main.py:1759) picks it up on next reload. Cost: loses pre-open grace (minor).
- **✅ CODE fix (restart-class, THE clean one):** line 726 change `_held_eff_secs < _min_hold_floor` → `0.0 <= _held_eff_secs < _min_hold_floor`. Negative (bogus/future anchor) → guard False → NOT suppressed → old bugged position can exit; fresh [0,60) still protected.
- ⚠️ Do NOT also apply the agent's line-721 `min(lag, held)` clamp — it zeros an OLD position's held_eff to 0, which `0<=0<60` then RE-suppresses. Line-726 alone is correct.

### C3. Legacy force-side / coin-flip floors — DONE
- **btc bitcoin_1h_simple_long** (bitcoin.py:1911 force LONG, :3578 `effective_min_edge=0` + flat 0.06): entry_min 0.55 staged (FROZEN, restart). ✅ partial.
- **alt_1h_simple_long NOT ported** — sol/doge/bnb still `entry_min: 0.5` (settings.yaml:857/1833/2048), force-edge-bypass via sol_macro.py:5995. eth/xrp disabled. → mirror the 0.55 fix (config, frozen).
- **BTC has NO center_price floor at all** (alts have ±0.02/0.12; eth 0.025). BTC min_edge 5m-up 0.05 admits 0.50 coin flips. Gap.
- Both simple_long set `effective_min_edge=0.0` — no absolute-edge/distance-from-0.50 guard. Removing the bypass entirely = risky (btc 1h is historically a WINNER via this lane when 4H bull) → DEFER to shadow, don't blind-remove.
- eth_5m_buy_no_flip_to_yes = default ON (eth_macro.py:2056) — force-side, keeps min_edge. Note.
- Vestigial keys confirmed dead (safe to ignore).

---

## PART D — PRIORITIZED FIX PLAN (synthesis)

**IMPLEMENT (high-confidence, backup + Codex-review + py_compile, stage for morning restart):**
- **F1 🔴 B1 exit-suppression** — live_testing.py:726 guard `0.0 <= held_eff`. THE bleed fix. + document the hot kill-switch as emergency lever.
- **F2 🔴 B2/B3 CLOB status** — clob_client.py:1169 normalize case, treat MATCHED/MINED as filled + always trade-recover before pending.
- **F3 🟠 alt_1h_simple_long.entry_min 0.5→0.55** sol/doge/bnb (config, frozen) — mirror btc.
- **F4 🟠 Concentration guard** — RiskManager net-same-side crypto cap (the "6 correlated shorts" gap). NEW, conservative, heavy Codex review.

**DEFER — design as proposals (need shadow/care, higher risk):**
- D1 Oracle basis directional/cost-model (settlement risk; ghost-validate) — big frequency win but careful.
- D2 Remove simple_long edge bypass (could kill the winning btc 1h bull lane) — shadow first.
- D3 centered_price / frequency tuning (freq vs quality tension).

## PART E — PAPER-TEST PLAN
Run `src/main.py --paper --no-dashboard` (separate port) after fixes staged; confirm: (a) no held-eff suppress spam, exits fire; (b) entries fill; (c) no concentration >cap; (d) py_compile clean. Do NOT touch the live process.

### C2. Scanner / oracle_basis / concentration — DONE
- **oracle_basis_block (31) = mostly FALSE skips.** basis = (exch_spot − Chainlink)/oracle bps (updown_composite_score.py:145). −32..−36 bps against a *fresh-flagged* oracle, correlated one-directional across ALL 6 assets in a bearish tape = **Chainlink deviation-lag** (feeds post on ~0.5%/50bps deviation+heartbeat, so a "fresh" oracle can sit ~50bps stale), NOT arbitrage. Caps (doge18/eth20/xrp20/sol25/bnb30/hype40) + relax ceilings (22–30) sit BELOW Chainlink's ~50bps band → hard-block. It blinds the bot exactly when its down-read is strongest. **NO `_buy_no` per-side override exists** (only sol/bnb 15m_buy_yes). Two gates exist (sol_macro.py:2229 full + 2222 simple@5076) — must fix both.
  - Fix options (NOT a blind loosen — settlement risk): (a) **directional** — only block when basis sign works AGAINST the side (for a SHORT, spot-below-oracle actually FAVORS it as the oracle catches down); (b) hype's `oracle_ref_use_exchange_spot`/`oracle_stale_spot_is_settlement` pattern (validate vs fresh exch mid); (c) Codex's basis-COST model `edge ≥ min_edge + |basis|/1e4 + fees`. Careful — needs ghost validation.
- **centered_price_edge_below_min (9 xrp) = over-tight OUTLIER.** xrp `min_edge_when_centered: 0.12` (settings.yaml:1610) vs eth 0.025 — 5× tighter. Demands 12% edge at coin-flip prices. Lower toward family (0.025–0.06). Hot config.
- **🔴 CONCENTRATION RISK — real & UNGUARDED (highest-value safety add).** NO cross-asset directional/correlation cap anywhere. Only: global count cap `max_concurrent_positions`=10 (direction-blind), per-strategy count/$ caps, per-lane ExposureManager breaker. `_build_correlation_context` (main.py:5312) is informational post-trade AI only — never blocks. → **6 simultaneous crypto SHORTs = ~6 identical BTC-beta bets budgeted as independent** (this is the "doubled down on all the same trades" the operator saw). FIX: family-level net-same-side exposure cap in RiskManager.can_trade/evaluate_entry, keyed on crypto-updown family + side, using active_positions (carry strategy+action). NEW feature, restart-class.
- Legit gates (leave): lane_entry_window (timing), buy_no_15m_disabled_lane (ghost −EV sit-out), lane_min_edge (n=5 healthy; consult performance_feedback.check_overtight not hand-tune).

### C3. Legacy force-side / coin-flip floors (agent + Codex)
_pending_

### C4. Codex independent pass — DONE (high quality)
1. **[CRIT] Exit-suppression root cause PINNED** — live_testing.py:293 & :720. `_held_eff_secs = held_secs − _preopen_lag_secs(pos)`; `_preopen_lag_secs = (end_date − opened_at) − window_size`. Algebra → **held_eff = now − (end_date − window_size) = now − window_open**. If a reloaded position's `market_end_at` is future/wrong, held_eff goes hugely negative → suppresses stop/TP/never_green FOREVER. FIX: anchor min-hold to actual `opened_at` fill time; only window-anchor fresh pre-open entries with validated `window_open ≤ now+grace`; bypass min-hold for resumed positions already older than the floor.
2. **[CRIT] Feeder** — main.py:1906 `_sync_journal_to_risk_manager()` restores `market_end_at` from journal WITHOUT validating vs current market/window → feeds the bad future anchor on resume. FIX: on reload, refetch market by id, validate `end_date−opened_at ≈ window_size`; if impossible set end_date=None + disable window-anchoring for that pos.
3. **[CRIT] CLOB MATCHED→pending** — clob_client.py:1169 only recognizes lowercase "filled"/"cancelled"; "MATCHED" returns PENDING. FIX: normalize case; treat FILLED/MATCHED/MINED as filled when size fully matched; always call `_recover_status_from_trades()` for non-terminal before returning pending.
4. **[HIGH] Trade-recovery misses variant ids/status** — clob_client.py:1252 narrow key set + only `order_id`; payloads carry id/orderID/orderId, nested maker/taker, status=MINED. FIX: canonicalize all id variants; classify MINED/MATCHED/CONFIRMED as filled.
5. **[HIGH] simple_long bypass admits zero-edge** — bitcoin.py:1911,3578; sol_macro.py:5995. Forces LONG, sets effective_min_edge=0, overwrites edge w/ sizing credit. FIX (beyond my entry_min 0.55): remove the min_edge bypass — require real lane min_edge on calibrated prob, or shadow-only until evidence.
6. **[HIGH] Other edge-bypass flip/exempt paths** — sol_macro.py:6013,4677; eth_macro.py:2056 change side and/or lower effective_min_edge on tape labels. FIX: after ANY side flip, recompute est_prob/edge/lane-policy/min_edge from scratch; no flat edge credits.
7. **[HIGH] Oracle basis gate too binary** — updown_composite_score.py:145,186; config:1841. basis = exchange_spot − Chainlink, hard-blocks >18–25bps; observed 32–36bps may be REAL settlement risk not stale. FIX (don't blindly loosen): basis-COST model — require `edge ≥ min_edge + |basis|/10000 + fees/slippage`; higher hard cap only after ghost validation.
8. **[MED] Oracle telemetry underreports per-window cap** — sol_macro.py:4262 logs global cap not resolved per-window/side. FIX: log resolved cap+relax+window+side+spot src+age.

---

## PART D — PRIORITIZED FIX PLAN  [PENDING synthesis]
_pending_

## PART E — PAPER-TEST PLAN  [PENDING]
_pending_
