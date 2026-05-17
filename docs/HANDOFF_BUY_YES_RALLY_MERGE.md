# Handoff: BUY_YES recovery + dual-direction rally logic

**Date:** 2026-05-17  
**Audience:** Next agent (Cursor / Codex / Claude)  
**Plan source:** `.cursor/plans/buy_yes_rally_merge_4b6efcaa.plan.md` (same content below)  
**Branch:** `main` (local workspace `psb-main 1`)

---

## Executive summary

Paper trading degraded after ~May 9 when **`buy_no_ltf_override`** could flip **BULL** macro scans to **SHORT** without bullish confirmation, clashing with the **BUY_YES** path that was working well May 3–8 (heavy **take_profit** on BUY_YES per [`docs/session_reports/attribution_since_may4_2026_mtime.json`](session_reports/attribution_since_may4_2026_mtime.json)).

**Do not blind-revert** to `35116dd^`. Old code had hard alt-1H blocks and histogram skips that hurt BUY_YES too. **Apply targeted fixes:** keep post-May-8 improvements that help BUY_YES, fix the buy_no clash, add **symmetric LTF momentum on both directions in both regimes**.

---

## Operator intent

| Regime | Default priority | Exception | Momentum on both sides |
|--------|------------------|-----------|-------------------------|
| **BEAR** | BUY_NO / SHORT | Rally → BUY_YES / LONG | SHORT needs **bearish** LTF; LONG needs **bullish** rally |
| **BULL** | BUY_YES / LONG | Dip → BUY_NO / SHORT | LONG needs **bullish** rally; SHORT needs **bearish** dip |

**Priority order for implementation:**

1. **Phase A** — Fix BUY_YES path (gates, clash with buy_no, keep good helpers).
2. **Phase B** — Four-path resolver + symmetric momentum helpers.
3. **Phase C** — Config + comments in `settings.yaml`.
4. **Phase D** — Backtest parity + pytest + paper compare.

---

## What to keep vs fix (audit guide)

### Keep (post–May 8, BUY_YES-helping)

- `flat_btc_alt_aligned_bypass` when alt 1H BULLISH
- Alt-1H BUY_YES **diagnostic-only** (no hard `buy_yes_suppressed` skip)
- Histogram **dampening** vs hard block (unless audit proves hard block was better for BUY_YES TP cohorts only)
- `scripts/compare_paper_to_backtest.py` for verification

### Fix (BUY_YES-hurting or incomplete)

| Issue | Where | Fix |
|-------|--------|-----|
| BULL macro flips to SHORT before action | [`src/strategies/sol_macro.py`](src/strategies/sol_macro.py) `_buy_no_ltf_override` ~L436, side block ~L1411–1495 | Subordinate buy_no to bull default LONG when bullish rally confirms |
| No symmetric bullish rally in bear | `sol_macro.py` | Add `macd_bullish_momentum_ok` + `_bullish_rally_ltf_ok` |
| Bear default SHORT without bearish momentum | resolver (new) | Require `_bearish_dip_ltf_ok` for all SHORT paths |
| Bull default LONG without bullish momentum | resolver (new) | Require `_bullish_rally_ltf_ok` for all LONG paths |
| Backtest drift | [`src/backtest/updown_engine.py`](src/backtest/updown_engine.py) | Mirror resolver + `_bullish_rally_ltf_replay` |
| ETH path | [`src/strategies/eth_macro.py`](src/strategies/eth_macro.py) | Inherits parent; confirm scan does not bypass resolver |

### Do not revert without proof

- **Alt-first** `primary_htf_bias` (May 12 `e1b98db`) — only change if audit/backtest shows BUY_YES count recovers with BTC-first.
- Disabling `buy_no_ltf_override` entirely.

---

## Four-path resolver (implement in `sol_macro.py`)

New function: `_resolve_allowed_side_with_ltf_overrides(ta, primary_htf_bias) -> (side, side_source)`.

```text
1. Regime default: BEAR → SHORT, BULL → LONG
2. BEAR default SHORT: admit only if _bearish_dip_ltf_ok → side_source bearish_dip_default
3. BULL default LONG: admit only if _bullish_rally_ltf_ok → side_source bullish_rally_default
4. BEAR exception LONG: if _bullish_rally_ltf_ok and wins over bearish dip → bullish_rally_exception
5. BULL exception SHORT: if _bearish_dip_ltf_ok and NOT _bullish_rally_ltf_ok → bearish_dip_exception
6. Chop (both momentums): regime default if its momentum passed; else skip — log ltf_momentum_ambiguous
```

**Clash rule:** In BULL, **buy_no cannot fire** when bullish rally confirms default LONG.

**Helpers:**

- `macd_bearish_momentum_ok(m)` — exists ~L87
- `macd_bullish_momentum_ok(m)` — add (mirror bearish)
- `_bearish_dip_ltf_ok(ta)` — align with current `_buy_no_ltf_override` thresholds
- `_bullish_rally_ltf_ok(ta)` — 15m+5m bullish MACD, RSI ≥ min, BTC 5m ≥ min

Wire `side_source` into skip diagnostics / journal metadata where other strategies log `primary_htf`.

---

## Config (`config/settings.yaml`)

Under each macro strategy block (`sol_macro`, `eth_macro`, `xrp_macro`, `hype_macro`):

```yaml
# Bullish rally — ALL LONG paths (bull default + bear exception)
buy_yes_ltf_override_enabled: true
buy_yes_ltf_override_rsi_min: 55.0
buy_yes_ltf_override_min_btc_5m_pct: 0.0

# Bearish dip — ALL SHORT paths (bear default + bull exception)
buy_no_ltf_override_enabled: true
buy_no_ltf_override_rsi_max: 45.0
buy_no_ltf_override_max_btc_5m_pct: 0.0
```

Comment in yaml: both directions use LTF momentum; neither side is naked macro-only.

---

## Tests (must pass before done)

```bash
cd "/Users/mainfolder/Documents/psb-main 1"
.venv/bin/python -m pytest tests/test_sol_macro.py tests/test_eth_macro.py tests/test_updown_backtest_parity.py -v
```

Add cases:

- BULL + bullish rally → LONG; buy_no blocked
- BULL + bearish dip only (no rally) → SHORT exception
- BEAR + bearish dip → SHORT default
- BEAR + bullish rally → LONG exception
- Ambiguous chop → default or skip per rules

---

## Verification (after implementation)

1. **Paper compare** (May 3–8 sessions if journals exist):

   ```bash
   .venv/bin/python scripts/compare_paper_to_backtest.py --help
   ```

   Target: BUY_YES count and **take_profit share** move toward May 4 baseline; fewer naked BUY_NO in rips.

2. **Crypto backtest** (local, cached OHLCV):

   ```bash
   .venv/bin/python scripts/run_backtest_crypto.py --symbol ETH --window 15 --start 2026-01-20 --end 2026-04-20
   .venv/bin/python scripts/run_backtest_crypto.py --symbol SOL --window 15 --start 2026-01-20 --end 2026-04-20
   ```

3. **Strategy log:** append Change Log to `projects/polymarket-bot/strategy-log/sol_macro.md` + `eth_macro.md` (status `pending` until 15+ closed trades).

4. **Agent index:** one line in [`docs/AGENT_CHANGELOG.md`](AGENT_CHANGELOG.md) when shipped.

---

## Success criteria

- BUY_YES path no longer loses to bull-macro buy_no flips on rally tape.
- **Both** LONG and SHORT admissions require their momentum gate in **both** regimes.
- buy_no remains a **secondary** bull dip exception.
- Live and [`updown_engine.py`](src/backtest/updown_engine.py) replay use the same resolver order.
- Pytest green; paper/backtest show improved BUY_YES TP mix vs current calibration (`data/calibration/trades.jsonl` is mostly post–May 9 BUY_NO).

---

## Out of scope

- [`src/strategies/bitcoin.py`](src/strategies/bitcoin.py) rally logic unless requested.
- Railway deploy / hosted bot restart (operator uses local dashboard per `docs/LOCAL_BOT_RUN.md`).
- Reverting alt-first without audit-backed BUY_YES benefit.

---

## Key git anchors

| Commit | Date | Note |
|--------|------|------|
| `35116dd^` | ~May 8 | Last good BUY_YES era baseline (BTC-first, no buy_no flip) |
| `35116dd` | May 9 | Introduced `buy_no_ltf_override` |
| `e1b98db` | May 12 | Alt-first direction |
| `24a3812` | May 4 | Brief bearish MACD exception for BUY_NO (removed May 5) |

---

## Related docs

- [`docs/STRATEGY_ENTRY_SPEC.md`](STRATEGY_ENTRY_SPEC.md) — entry/sizing (mispricing rules)
- [`docs/HANDOFF_STRATEGY_ENTRY_AND_BACKTEST.md`](HANDOFF_STRATEGY_ENTRY_AND_BACKTEST.md) — live/backtest alignment
- [`docs/polymarket-backtest-subagent-skill.md`](polymarket-backtest-subagent-skill.md) — audit methodology after material changes
- [`CURSOR_HANDOFF.md`](../CURSOR_HANDOFF.md) — ETH lag / older checklist (separate feature set)

---

*End of handoff — implement phases A→D in order; do not skip Phase A clash fix before adding new overrides.*
