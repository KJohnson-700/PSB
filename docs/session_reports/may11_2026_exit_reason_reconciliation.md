# May 11 2026 — exit audit reconciliation (journal vs code)

**Purpose:** Align operator language with implementation so audits do not conflate **`updown_stop_loss`** (percentage stop) with **`updown_time_stop`** (late-window cents stop).

## Code mapping (authoritative)

| Journal `exit_reason` | YAML / config driver | Implementation |
|----------------------|----------------------|----------------|
| **`updown_stop_loss`** | `trading.exit_rules.updown_stop_loss_pct` | [`PositionExitManager.check_exits`](../../src/execution/live_testing.py) — fires when unrealized **PnL %** vs cost basis ≤ **−`updown_stop_loss_pct`** (default **0.20**). Evaluated **before** the late-window cents branch. |
| **`updown_time_stop`** | `updown_stop_cents` + `updown_exit_window_mins` (+ optional `updown_exit_window_max_fraction`, high-entry cents override) | Same file — only when **minutes to expiry ≤ effective exit window** and price moved **≥ `updown_stop_cents` against the held leg**. |

Backtest replay uses the same labels in [`UpdownBacktestEngine`](../../src/backtest/updown_engine.py).

**If an audit cites “`updown_stop_cents` / final 2.25 min” as the mechanism for `updown_stop_loss` rows, that attribution is wrong for this codebase** — those rows are the **% stop**. Late-cents exits should appear as **`updown_time_stop`**.

## Repo paper data: `test_20260511_*` sessions

Sliced with:

```bash
.venv/bin/python scripts/slice_paper_exits_by_session_prefix.py --prefix test_20260511
```

**Sessions:** `test_20260511_153650`, `test_20260511_154851`, `test_20260511_224145` (only the latter two contributed EXIT rows in `entries.jsonl` at the time of this report).

| exit_reason | n | net PnL (USD) | notes |
|-------------|---:|---:|---|
| `updown_stop_loss` | 4 | −9.04 | **% stop** (`updown_stop_loss_pct`), not 3¢ late window |
| `take_profit` | 3 | +7.98 | |
| `updown_time_stop` | 0 | 0.00 | No late-window cents stops in this slice |

**By window (question time-range delta):**

- `updown_stop_loss`: **5m** n=2, PnL −6.43; **15m** n=2, PnL −2.61  
- `take_profit`: **5m** n=2; **15m** n=1  

**Mismatch vs informal “17 exits / 0 wins” memo:** the checked paper folders only contain **7** EXIT lines for that prefix. If the May 11 audit used a different export (Railway journal, merged sessions, or a wider date range), re-run this script with the correct `--prefix` or extend it to multiple prefixes.

## Strategic read (unchanged from plan)

- **`updown_stop_loss` dominating** in this slice means positions hit **~−20%** unrealized vs basis before expiry — consistent with **weak entries / adverse drift**, not “2 minutes of 3¢ noise” alone.
- When **`updown_time_stop`** dominates (other sessions), the **cents + short window** narrative applies; see [crypto_strategy_audit_current_vs_last_20260508.md](crypto_strategy_audit_current_vs_last_20260508.md) for tension vs **`updown_stop_loss_pct`** tightening.
- **Entry-first tuning** (especially **5m** lanes): prefer `min_edge_5m`, catalyst gates, and cohort review before loosening **`updown_stop_loss_pct`** without evidence ([May 8 audit](crypto_strategy_audit_current_vs_last_20260508.md) F1).

## Related tooling

- [`scripts/slice_paper_exits_by_session_prefix.py`](../../scripts/slice_paper_exits_by_session_prefix.py) — repeatable slice for any `test_YYYYMMDD_*` prefix.  
- [`src/analysis/underperformance_audit.py`](../../src/analysis/underperformance_audit.py) — JSON/Markdown diagnosis now surfaces **separate** BUY_YES loss share for **`updown_time_stop`** vs **`updown_stop_loss`** in the overall summary.
