# RECURRING-ISSUE LEDGER — standing fault classes (check every session audit)

Purpose: stop re-discovering the SAME faults. Each row is a fault *class* that has
bitten more than once, its detection signal, and current status. Add a row the
moment a fault recurs; never delete — mark RESOLVED with the fix.

Rule: a fault that recurs after being "fixed" means the fix was narrow or the
detection wasn't wired. Wire the detector, don't just re-patch.

| # | Fault class | Detection signal (run each audit) | Status / last seen |
|---|---|---|---|
| R1 | **Feature silently dead after a dependency was removed** — e.g. `performance_feedback.kelly_mult` was wired to backtest `expectations`, which were removed 2026-05 → the whole loss/win-responsive sizing path returned 1.0 for weeks and nobody noticed. | grep for code gated on data that is now always empty/None (`expectations={}`, removed modules, `if not X: return <noop>`). Any adaptive feature: assert it actually FIRES (non-1.0 / non-default output) in a live/paper run, not just that it's "enabled". | **OPEN 2026-07-27** — kelly_mult dead. Superseded by `adaptive_lane_sizer` (realized-driven, not backtest). Audit other adaptive features next. |
| R2 | **Green stops** — fixed stop cuts a position that would have won if held. ~40% of stops, flat across cohorts, unmoved by narrow late-TP patches. | `python -m src.analysis.lane_exit_policy --print` → count Policy-A "exit kills edge" lanes + green-stop %. | **OPEN 2026-07-27** — 40% green-stop rate. First real fix staged: eth 1h BUY_YES hold+trail. |
| R3 | **Exit suppression via bad held-eff anchor** — resumed sessions carry a future `end_date` → held_eff negative → stops/TP/never_green_cut all suppressed → losers ride to full loss. | grep live log `Suppress .* held-eff -` count climbing. | **FIXED (staged) 2026-07-27** — F1 guard `0.0 <= held_eff < floor`. Not yet live (needs restart). |
| R4 | **Coin-flip clusters** — one lane/side stacks many near-0.50 entries into an oversold/overbought fade (e.g. eth 1h BUY_NO into RSI-16), concentrating capital in a near-random bet. | per-tick: deployed-capital concentration by lane/side; flag any lane > ~50% of book at entry_price ~0.48–0.52. | **WATCH 2026-07-27** — eth 1h BUY_NO 4×/67% of book (paper, near-flat). Concentration-guard deferred until it costs money. |
| R5 | **CLOB status misreport** — filled orders reading `MATCHED`/`MINED`/`CONFIRMED` classified as PENDING → stranded exits, ride-to-zero. | grep filled-but-PENDING loops; reconciler phantom count. | **FIXED (staged) 2026-07-27** — F2 status map. Not yet live. |
| R6 | **Scanner starvation** — dead fetch left ON (hype_alt while hype disabled) eats slug budget; inner slug loop had no timeout → one hung slug drops a whole window all-or-nothing. | ops_pulse `updown_hype_alt_count` (should be 0 when hype off); slug coverage 5m/15m/1h; outer 35s-timeout count. | **FIXED (staged) 2026-07-27** — S1 drop dead fetch, S2 inner timeout, S3 adaptive order. Live in paper. |
| R7 | **Feature shipped OFF / dormant** — a built feature sits `enabled: false` or staged-not-live and is mistaken for working (tape_freshness / lane_tape_adapter). | audit `enabled:`/`disable_*` flags vs what's claimed live; "staged" ≠ "live". | **OPEN** — tape adapter dormant. Adaptive-to-tape not truly done. |
| R8 | **Dashboard shows stale/disabled state as active** — lane-gates card ignores master `enabled:false` (hype shows "active" though it never trades). | eyeball card vs `strategies.<x>.enabled`. | **FIXED 2026-07-27** — `_build_lane_gates` now marks disabled strategies closed (kind=strategy_disabled). Live. |
| R9 | **Dashboard card built with no data producer** — Break-Trigger Board card + `/api/triggers` endpoint existed since 2026-07-16 but nothing ever wrote `break_triggers.json` → perma-blank. | curl `/api/triggers` → `error:"no break_triggers.json"`; grep repo for a WRITER, not just a reader. | **FIXED 2026-07-27** — `break_trigger_board.py` generates the file; endpoint self-heals. 11 lanes populate. |
| R10 | **Shadow that never graduates / naive enforce would lose** — per-lane breaker fires but `execution_enforcement_enabled:false`. Tempting to just flip it on. | before flipping ANY shadow→enforce: compute forward-P&L after each fire point; only enforce if net-positive. | **VERIFIED 2026-07-27** — blanket enforce nets **−$34** (recoverers like xrp5m-NO +$57 get cut). Correctly KEEP shadow. |

## How to use
1. Every session audit: run the detection signal for each OPEN/WATCH row.
2. If a FIXED row's signal reappears → the fix was narrow; reopen and widen, don't re-patch.
3. New recurring fault → new row with a detection signal *before* patching, so it self-catches next time.
