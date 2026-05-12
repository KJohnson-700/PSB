# Handoff for Codex: 30m up/down chain + dashboard TA (review pass)

**Date:** 2026-05-11  
**Repo:** PSB (`main`; verify `git remote -v` → `https://github.com/KJohnson-700/PSB`)  
**Intent:** Second pair of eyes on the batched edits below: correctness, regressions, and whether stated **reasons** and **expected outcomes** hold.

---

## 1. Why these changes were made

| Area | Reason |
|------|--------|
| **Scanner / classification** (`scanner.py`, `live_strategy_scan.py`, `run_backtest_crypto.py`) | Carry **30m** Polymarket buckets through discovery and labeling so live/backtest agree on window family (`*_updown_30m` et al.). |
| **Strategies** (`bitcoin.py`, `sol_macro.py`, `eth_macro.py`, `main.py`) | Align entry/guard paths with **30m** markets and short-window rules already discussed for the 30m chain (including classification-driven behavior where applicable). |
| **Kelly / sizing** (`kelly_sizer.py`) | Expose **`_window_stats` / Kelly** behavior for **30m** where the Performance / audit surfaces expect it. |
| **Analysis services** (`sol_btc_service.py`, `btc_price_service.py`, `hyperliquid_hype_service.py`) | **Operator-facing truth:** same rows that showed 15m/5m MACD and %-moves should show **30m MACD** and **5/15/30m %** triples without new dashboard cards. HYPE on Hyperliquid needs a real **`30m`** interval mapping. |
| **Backtest** (`updown_engine.py`) | **`TechnicalAnalysis.macd_30m`** populated in replay so backtests don’t drift from live TA construction when that field is read. |
| **Dashboard API** (`server.py`) | JSON for **`/api/bitcoin/analysis`** and alt **`_solbtc_analysis_payload`**: `macd_30m_*`, **`btc_move_30m`**, **`sol_move_30m`**, **`{alt}_move_30m`**; crossover strings for BTC MACD tiers where the UI prints labels. Bump **`dashboard_ui_rev`**. |
| **Dashboard UI** (`index.html`) | BTC: extra hero + meta columns for **MACD 30m**; alts: **`MACD 30m \| 15m \| 5m \| Corr \| ATR`**; hero tiles show **Δ% 5·15·30m** from correlation payload. |
| **Tests** | Lock classification, Kelly, HYPE integration, and SOL macro expectations to the new surfaces. |
| **`docs/AGENT_CHANGELOG.md`** | Agent backfill per `AGENTS.md` (vault strategy log is separate). |

---

## 2. Suspected outcomes (what we expect if the implementation is correct)

1. **Hosted dashboard:** `GET /health` → **`dashboard_ui_rev`** matches `src/dashboard/server.py` (currently tied to the 30m/UI bump in this batch). Stale deploys show an **older** rev.
2. **BTC panel:** Hero shows **MACD 30m** and **MACD 15m**; meta strip lists **4H / 1H / 30m / 15m** MACD plus VP lines; values move when Binance (or resampled) data updates.
3. **Alt panels (SOL/ETH/HYPE/XRP):** Meta row shows five cells in order **30m, 15m, 5m, corr, ATR**; BTC and alt hero lines show **three %-move numbers** (5 / 15 / ~30m window from 1m closes).
4. **Correlation 1h:** Still computed from 1m returns; **30m %** uses **31×1m bars** (current vs **−31** close). If Binance returns **<31** 1m bars, 30m % may stay 0 — worth verifying under fetch failures.
5. **HYPE:** `fetch_klines(..., "30m")` resolves through Hyperliquid’s snapshot with **`30m`** ms stride — **no silent fallthrough** to wrong interval.
6. **Strategies / scanner:** No accidental disabling of lanes; **30m** slugs classify and hit the same risk/ops gates as intended; **no extra Discord opportunity webhooks** (execution-only rule unchanged).
7. **Tests:** `pytest` on touched modules stays green; optional full suite before deploy.

---

## 3. Codex review checklist (concrete)

- [ ] **API contract:** `get_bitcoin_analysis` returns **`macd_1h_*`**, **`macd_30m_*`**, **`macd_15m_*`**; crossover fields are **strings** (not coerced bools) for UI display.
- [ ] **Alt payload:** `btc_move_30m`, `sol_move_30m`, `{alt_code}_move_30m` present and consistent with **`sol_move_*`** legacy keys.
- [ ] **Resampling:** When native **30m** fetch fails or is empty, **15m→30m** resample logic matches live MACD warmup (**≥30** bars before non-default `MACDResult()`).
- [ ] **`updown_fills.jsonl`:** Not required for this review unless operators intentionally version entry-price logs — confirm whether local data commits are desired (left **unstaged** in the commit that accompanies this handoff unless you add it).
- [ ] **Regression scan:** Grep for **`macd_30m`** / **`TechnicalAnalysis(`** in tests and backtest paths — any constructor missing the new field should still work via **`default_factory`** where applicable.
- [ ] **`dashboard_ui_rev`:** Single bump covers both Journal/WR and 30m TA changes or note if deploy order matters.

---

## 4. Suggested commands for the reviewer

```bash
# from repo root, with .venv
.venv/bin/python -m pytest tests/test_sol_macro.py tests/test_eth_macro.py \
  tests/test_updown_backtest_parity.py tests/test_bitcoin.py tests/test_classify_updown.py \
  tests/test_kelly_sizer.py tests/test_hype_integration.py -q

# optional: full suite
.venv/bin/python -m pytest -q
```

---

## 5. After review

- If anything is wrong: prefer a **small follow-up commit** with a one-line note in `docs/AGENT_CHANGELOG.md` (append only).
- Strategy **hypothesis / 15+ trade outcomes** still belong in the Obsidian strategy log per `AGENTS.md`, not only here.
