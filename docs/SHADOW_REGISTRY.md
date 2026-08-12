# SHADOW REGISTRY — the marker so shadows stop getting orphaned

**Rule (operator 2026-08-12):** every shadow is EITHER (a) live + scored + tracked here, OR (b) removed if redundant/failed/unneeded. No silent graveyard. Re-check this file whenever a shadow is built or a decision is revisited.

Score a shadow = join its jsonl to realized outcomes and compare vs the champion/coinflip. If it beats coinflip at n≥~30 → promote/wire. If ≤ coinflip → CULL.

## KEEP — live + scored/tracked

| Shadow | Tests | Status / score | Decision |
|---|---|---|---|
| **ai_direction** (`ai_direction_engine.py` + `psb_direction_driver.py`) | qwen_vision / minimax chart+tape direction vs tape_map champion | **SCORED 08-12 (n=18,118): qwen_vision 50.6% > champ 47% > minimax 43.8%.** | **PROMOTED**: qwen_vision→primary in driver; `direction.enforce:true` (drives on next bot restart). Re-score weekly via `ai_direction_score.py`. |
| **side_resolver_v2** (`side_resolver_v2.py`, `side_resolver_v2_shadow.py`) | single-owner auditable side resolver vs the 6-stage champion | shadow-only; can't fully score (candidate log lacks tape_dir/adapter — needs live per-candidate emit). Champion measured 50.8% (eth 43%). | KEEP — build the live emit to finish scoring, THEN wire as single owner (correctness win even if native stays coinflip). |
| **favorite_derisk_shadow** (`favorite_derisk_shadow.py`, pid live) | would-be favorite de-risk exits | live loop | KEEP while favorite lane logic evolves; re-eval when favorites stay off. |

## CULL — tested-dead / stale / redundant (remove script + archive jsonl)

| Shadow | Why cull | Action |
|---|---|---|
| **cex_pm_lag** (`cex_pm_lag_shadow.py`, jsonl 15MB) | **TESTED DEAD**: Binance edge over PM mid = −0.000 (48,585 rows). PM mid is efficient. | daemon KILLED 08-12 (was pid 4099). Archive script+jsonl. |
| **btc_alt_leadlag** (`scripts/btc_alt_leadlag.py`) | **TESTED DEAD**: cross-corr peaks at lag 0 (simultaneous, no lead). | Archive. |
| **neutral_sitout_shadow** (jsonl 26MB, last Jun 19) | stale ~2mo, superseded by tape/regime work. | Archive jsonl. |
| **window_delta_shadow** (jsonl 87MB, last Jun 19) | stale ~2mo; window_delta logic long since shipped/changed. | Archive jsonl. |
| **exit_excursion_shadow** (Jun 19), **cut_reopen_shadow** (Jul 23), **floor_release_shadow** (Jul 24), **time_underwater_shadow** (Aug 10), **eth_posterior_gate_shadow** (Aug 9, 578B) | stale/tiny, superseded by the MFE-conditional stop + hold_all exit work. | Archive jsonl. |

## TRIAGE — decide next (huge or unclear; do NOT delete blind)

| Shadow | Size / status | Open question |
|---|---|---|
| **attribution_shadow** (566MB!, Aug 9) | dynamic-admission attribution | Is it feeding any live decision or just growing? If unused → cull. Rotate/cap the file regardless (566MB is a disk risk). |
| **btc_neutral_resolver_shadow** (186MB, still writing) | btc neutral-resolver right-side | Score it; if ≤ coinflip, cull. Cap the file. |
| **rotation_shadow** / **directional_breaker_shadow** (Aug 9) | dynamic-admission rotation + breaker | Part of the "dynamic admission" stack — score or cull as a set. |
| **tape_side_veto_shadow** (Aug 12, writing), **entry_book_shadow**, **adaptive_sizer_shadow**, **tape_stop_shadow**, **realized_kelly_shadow**, **tape_entry_shadow**, **never_green_shadow**, **exit_ai_shadow**, **topup_shadow**, **hold_benefit_shadow** | mixed | one-line score each; keep the ones that beat their champion, cull the rest. |

## Daemons (nohup) inventory — 2026-08-12
- `ai_direction_engine.py` (pid 85139) — providers minimax_tape,qwen_vision; interval 300s. KEEP.
- `psb_direction_driver.py` (relaunched 08-12, qwen-primary). KEEP.
- `cex_pm_lag_shadow.py` — KILLED (dead).
- `favorite_derisk_shadow.py` (pid 10587). KEEP.
