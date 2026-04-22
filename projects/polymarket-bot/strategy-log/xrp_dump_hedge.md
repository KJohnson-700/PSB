# XRP dump-and-hedge (`xrp_dump_hedge`)

Polymarket **15m XRP Up/Down** — leg1 buys YES on a sharp book dump (optional **BTC 5m return z-score** gate); leg2 buys NO when **YES mid + NO mid ≤ max_pair_cost** (box). **Quant only; no LLM.** Gamma slugs: `xrp-updown-15m-*`.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | - | `/api/journal/summary` |
| Win rate | - | same |
| Net PnL | - | same |
| Notes | Default **disabled** in `config/settings.yaml` until live validation | config |

## Change Log

### 2026-04-09 — Initial implementation (code + sim backtest)

- **What changed:** Added `XRPDumpHedgeStrategy` (`src/strategies/xrp_dump_hedge.py`), scanner `xrp-updown-15m` slugs, `main.py` fast-loop execution (BUY_YES / BUY_NO), isolated exposure slot, risk manager crypto bucket, dashboard backtest entry **XRP Dump-Hedge (sim)** → `scripts/run_backtest_xrp_dump_hedge.py`, unit tests `tests/test_xrp_dump_hedge.py`. Config block `strategies.xrp_dump_hedge` (starts `enabled: false`).
- **Why:** Port Matias-style dump-and-hedge **architecture** with a **BTC-led z-gate** instead of XRP-only book noise; keep execution aligned with existing CLOB/journal patterns.
- **Hypothesis:** Short-lived YES dislocations after macro impulse (BTC z) plus completion when paired mids sum below parity improve risk-adjusted expectancy vs. one-leg dump chasing alone.
- **Expected outcome:** Small number of high-conviction two-leg boxes; controlled size via `kelly_fraction` and crypto position caps.
- **Actual outcome:** `pending` (need ≥15 closed trades after enabling live/paper with real `xrp-updown-15m` liquidity).
- **Status:** `pending`

## Review sessions

_(none yet)_

## Lessons learned

_(none yet — add only after data)_
