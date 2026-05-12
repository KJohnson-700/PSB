# Agent changelog (backfill)

**Purpose:** Record **what shipped** when work was done in **Claude Code, Codex, Cursor**, or similar **without** a matching entry in the Obsidian strategy log or a written operator handoff. **Git remains the source of truth**; this file is a readable index.

**Strategy tuning and hypothesis tracking** still belong in `projects/polymarket-bot/strategy-log/` per `AGENTS.md`. This doc covers **codebase / infra / dashboard** provenance only.

**Canonical repo for this bot:** `https://github.com/KJohnson-700/PSB` (see `AGENTS.md` — do not confuse with other GitHub projects).

---

## 2026-05-12 — strategies/_core: live↔backtest decision-logic unification

**Why:** `src/backtest/updown_engine.py` previously hand-copied ~2000 lines of
entry-decision logic from `BitcoinStrategy` / `SolMacroStrategy` /
`ETHMacroStrategy`. Five drift bugs were found in the process of unifying it,
all now fixed. From this commit forward, any change to a strategy decision
helper **must go in `src/strategies/_core/`** so live and backtest stay
in lockstep.

**`src/strategies/_core/`** modules (each is a pure function with no IO):

- `htf_bias.py` — `btc_htf_bias` (3 callers: BitcoinStrategy, SolMacroStrategy,
  UpdownBacktestEngine)
- `ltf_strength.py` — `btc_ltf_strength_15m`, `sol_ltf_strength_15m`,
  `passes_15m_iql`, `passes_15m_iql_relaxed_rule`
- `m5_momentum.py` — `score_m5_direction` (BTC 5m), `sol_m5_macd_adj`
- `htf_boost.py` — `btc_5m_htf_boost`, `btc_15m_htf_boost`, `btc_5m_4h_1h_hist_gate`
- `timing.py` — `btc_15m_timing_bonus`
- `adjustments.py` — `rsi_4_level_adj_5m`, `rsi_4_level_adj_15m`, `sabre_tension_adj`
- `alt_gates.py` — `apply_primary_htf_bias`, `alt_1h_hist_gate`,
  `anti_ltf_gate_skip_reason`, `sol_rsi_extremes_adj`, `btc_catalyst_boost`
- `eth_follow.py` — `btc_follow_5m_impulse`, `btc_follow_15m_impulse_ok`,
  `eth_5m_macd_score`, `eth_15m_follow_score`

**Drift fixes shipped along with the extraction:**

1. **BTC 15m LTF threshold** — backtest used 0.35, live 0.50. Backtest was
   confirming pure-bull-cross signals (score 0.40) that live rejected. Both
   now use 0.50.
2. **BTC 5m 4H/1H histogram gate** — backtest had a hard 4H reject; live has
   a 1H momentum-recovery fallback. Backtest was rejecting entries live
   takes during 4H-decelerating / 1H-building windows. Both now share the
   fallback.
3. **BTC 15m HTF boost floor** — live had no floor on the graduated boost.
   When HTF=BULLISH was decided via recovery/early-bull votes (sabre=-1 +
   hist>0 below zero), the raw 3-vote lookup could yield a NEGATIVE
   `htf_boost`. Backtest had the +/-0.03 floor with a comment calling out
   the inconsistency. Live now applies the floor too.
4. **SOL/alt 15m 1H histogram gate** — backtest had a hard "must be rising"
   gate; live has a relaxed "block only when actively against" gate. Backtest
   was rejecting alt entries with positive-but-decelerating 1H histograms.
   Both now share the relaxed live form.
5. **ETH 15m follow scoring** — different tiers (backtest:
   0.06/0.04/0.02/0; live: 0.06/0.05/0/-0.05). Backtest had a 0.02 weak-MACD
   tier; live has a 0.05 strong-hist tier and a -0.05 against penalty. Both
   now use `eth_15m_follow_score`.

**Code-correctness fix:** `btc_price_service.calc_candle_momentum.m5_direction`
was undercoded — it only emitted SPIKE/DRIFT/empty, but BitcoinStrategy's 5m
path had LEAN_UP/LEAN_DOWN handler cases that never fired. Added LEAN tier at
`|move| > 0.01%` to the producer (and mirrored in
updown_engine._replay_candle_momentum / _calc_m5_momentum). Live now gets
the ±0.01 weak-nudge it always intended.

**Tests:** 293 pass. New parity tests in `tests/test_strategy_core_*.py`
lock both call sites for every extracted helper.

**Going forward — invariant:** any strategy-decision logic change must
either modify a `_core` helper (callers auto-update) or update **all**
existing callers in lockstep. Adding a new branch in `bitcoin.py` without
mirroring it in `updown_engine.py` will silently produce backtest results
that diverge from live.

## 2026-05-11 — Dashboard: real 30m MACD & % moves on existing rows

**SOL/BTC service:** `SOLAnalysis.macd_30m`, `BTCSOLCorrelation` 30m %-moves (1m×31), 120×1m pulls; **BTC** `TechnicalAnalysis.macd_30m` + **`get_full_analysis`**; **Hyperliquid** `"30m"` interval; **backtest** `_build_ta` populates `macd_30m`; **API:** `/api/bitcoin/analysis` macd 1h/30m/15m fields; alt payload `macd_30m_*`, `btc_move_30m`, `sol_move_30m`, `{alt}_move_30m`. **Dashboard:** BTC hero + meta strip MACD 30m; alt meta **MACD 30m \| 15m \| 5m \| Corr \| ATR**; hero **5·15·30m** Δ% triples. `dashboard_ui_rev` bumped.

## 2026-05-11 — Commit + Codex review handoff

Git commit batches the **30m** chain (scanner, strategies, Kelly, tests) and **dashboard/API/analysis** work; **`data/entry_prices/updown_fills.jsonl`** excluded from that commit (operator-local). Second-pass brief for reasons, expected outcomes, checklist: **`docs/HANDOFF_CODEX_30M_REVIEW_2026-05-11.md`**.

## 2026-05-11 — Updown WR on Live (existing gates only)

`/api/journal/updown_breakdown` is fetched when **Live** or **Performance** is open so **existing** Signal Gate WR lines (30m / 15m / 5m) populate; no extra Journal tab cards. `dashboard_ui_rev` bumped.

## 2026-05-11 — Up/down 30m chain + dashboard session P&L

Scanner merge + strategies already carried **30m** Polymarket buckets; this pass completes **dashboard classification** (`*_updown_30m`), **Performance TARGET** rows, **Kelly `_window_stats` 30m**, **SSE `session_total_pnl`** mapped into hero **Session P&L** (prefers journal **`total_pnl`** over realized-only), **`main.py`** lookahead logging + **`window_minutes ≤ 45`** crypto exemption + **`_detect_window_from_question`** 30m deltas, **`live_strategy_scan.py`** 9-tuple unpack, and test updates (`test_hype_integration`, `test_sol_macro`, classify/Kelly).

## 2026-05-10 — Dashboard session fills consistency

Journal: **`get_summary()`** **`total_entries` / `total_exits`** now mirror **`open_positions`** + **`closed_trades`** (phantom-skipped trades no longer deflate session fill totals on the dashboard). **`log_entry`** infers **`entry_leg`** from action/side/outcome when not passed explicitly (fixes **`BUY_NO`** defaulting to YES phantom guard at exit). **`_build_closed_stats`** token-flip filter matches **`log_exit`** (**YES-leg** only).

## 2026-05-09 — Session bundle (May 9)

Work landed on `main` the same calendar day; individual commits below are the audit trail where separate agent sessions did not leave vault notes.

| Commit | Summary |
|--------|---------|
| `1e4d433` | Drift runtime feedback (`performance_feedback`, `_runtime_feedback`), shared backtest expectations loader, strategy min-edge + Kelly hooks, dashboard drift/status + `STRATS` aliases + trade markers (darker/lighter in/out), journal learning module + `run_learning_loop.py` + tests. |
| `30d9252` | Dashboard: Claude design handoff visuals + live tab restructure. |
| `04f95b9` | Up/down backtest parity, sizing, narrators, session artifacts. |
| `add4724` | Dashboard: crypto tiles, decision gates, fullscreen layout. |
| `65a2c09` | Hide AI reasoning in dashboard summaries. |
| `6a24c8f` | Surface decision gates in dashboard. |
| `6afdc9a` | Oracle and composite decision gates. |
| `35116dd` | Use AI decision layer in macro strategies. |
| `0f28b69` | Enforced AI decision layer. |
| `1281189` | Require AI for low-confidence BTC neutral entries. |
| `d2299f7` | Tighten BTC neutral 15m entries. |
| `b417971` | Shadow calibration and exit replay tooling. |
| `c46e4f4` | Buy-no status diagnostics. |

---

## 2026-05-08 — AI / journal surface

| Commit | Summary |
|--------|---------|
| `acdbb3a` | Narrator output → journal + dashboard AI summary panel. |
| `b1c177a` | Auto-run narrators on bot startup against previous session. |
| `0e9d579` | Shadow pipeline expansion, post-trade annotation, narrators, pre-entry veto. |

---

## 2026-05-07 — Kelly, exits, dashboard wiring

| Commit | Summary |
|--------|---------|
| `e0718f5` | Docs: dashboard wiring sweep (stop-loss, exit-reason visibility). |
| `591266f` | Dashboard: stop-loss config, exit-reason summary, bankroll unavailable state. |
| `b0a89f8` | `updown_stop_loss_pct` (time-stop bleed). |
| `1e64a61` / `adc5157` / `7529062` / `1a7023d` | Kelly fraction calibration and cap fixes. |
| `cd66c96` | Config: alt-lane gate intervention. |
| `30061a3` | Lower `min_trade_usd` so Kelly sizing flows through. |

---

## How to extend

1. After a merge or push, append a **dated section** or add rows to the table.  
2. Prefer **one line per commit**; link to PRs if you use them.  
3. If you *did* update the strategy vault, cross-link the vault date/heading here (optional).
