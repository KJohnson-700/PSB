# Strategy log — index

Authoritative layout for strategy files in this vault. **Append only**; add new entries at the **top** of each section.
## One file per strategy

- `bitcoin.md`, `sol_macro.md`, `fade.md`, `neh.md`, `eth_macro.md`, `xrp_dump_hedge.md`, …

## Per-strategy file sections (top to bottom)

1. **Quick Stats** — table: metric / value / source (`/api/journal/summary`, backtest JSON). Do not estimate; use `-` if unknown.
2. **Change Log** — newest first. Each entry includes:
   - **What changed**
   - **Why**
   - **Hypothesis**
   - **Expected outcome**
   - **Actual outcome** — real data only; **minimum ~15 closed trades** after the change, else `pending`
   - **Status:** `pending` → `confirmed` (use checkmark in Obsidian) | `reverted` | `inconclusive`
3. **Review sessions** — short notes when triggered (15–20 new closed trades, strategy change, or regime shift).
4. **Lessons learned** — evergreen, data-backed; newest dated entries at **top**.

## Infrastructure / cross-cutting toggles

Not strategy tuning: record in `../changelog.md` (e.g. `ai.live_inferencing`, dashboard, deploy).

Optional: append-only **backtest audit notes** (after `docs/polymarket-backtest-subagent-skill.md` review) in `backtest_reviews.md` via `scripts/append_obsidian_backtest_review.py`.
