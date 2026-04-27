# XRP macro (`xrp_macro`)

XRP **Up or Down** — inherits shared `SolMacroStrategy` signal path with XRP market detection and `XRPUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 3 | recent paper session |
| Win rate | 100% | same |
| Net PnL | + | same |

## Change Log

### 2026-04-26 — Sample-size caution from first XRP macro batch

- **What changed:** No code/config change in this entry; this logs the first meaningful XRP macro paper slice.
- **Why:** Session produced only 3 XRP closes (all wins), which is too small to treat as validated edge.
- **Hypothesis:** Current XRP entries skew to higher edge (roughly 0.11–0.145) with earlier profit-taking, which can look strong in tiny samples but may not hold as sample broadens.
- **Expected outcome:** Keep strategy enabled and collect larger sample before changing gates or sizing.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades for first confidence pass).
- **Status:** `pending`

## Review sessions

### 2026-04-26 — Early paper read

- 3/3 wins is directional positive signal only; not statistically meaningful yet.
- Continue watching edge distribution and exit path behavior before tuning.

## Lessons learned

_(none yet — add only after data)_
