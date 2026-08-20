# CODEX REVIEW — exit orders ride binary positions to $0 (CLOB regression)

**Date:** 2026-07-27  **Severity:** HIGH (disables loss-cutting on all short-window lanes)
**Status:** DRAFT for Codex review. NOT applied. Restart-class (needs bot restart; hold until open position resolves + operator GO).

## Symptom
Dashboard flagged eth trades "riding to zero." Session `test_20260727_021601` (LIVE, direct-Polymarket-CLOB): the −$12.42 eth 15m BUY_YES (trade `…befa91`) went entry 0.49 → resolved NO at 0.00 = **full loss**. Its stop DID fire.

## Evidence (data/logs/polybot_20260727.log)
- `live_testing - INFO - Min-hold fresh-mark exemption: allowing updown_stop_loss for 3114625 …(ws mark age 3ms)` → `Exit manager: 1 positions ready to exit` — the **stop-loss exit fired** on a fresh mark.
- Exit order placed, then **65 consecutive** `Exit order accepted but not filled; keeping position open` / `Exit order still pending … status=pending` over ~13 min until the 15m market resolved to 0. = classic **GTC resting limit that never crossed**.
- A second position (`…cf4a76`) only filled after **~1.1h**: `Reconciled order … as FILLED from trade history`. So exits rest/pend far too slowly and miss resolution on short windows.
- 3 positions hit this today.

## Root cause (pinned)
`src/execution/live_testing.py:774–798`. Exit orders are placed FAK only when `ExitDecision.marketable` is True (consumed at `src/main.py:4210` — `order_type="FAK" if exit_decision.marketable else "GTC"`). Wiring is intact (`ExitDecision(marketable=exit_marketable)` at live_testing.py:942 → `check_exits` → `_handle_exit_decision`).

The bug is the CONDITION for a stop:
```python
exit_marketable = False
if reason == "updown_stop_loss" and exec_exit_price is not None:   # <-- gate
    exit_price = exec_exit_price
    exit_marketable = True
elif reason in ("never_green_cut","updown_flatten_pre_resolution",
                "updown_expired","updown_time_stop","take_profit_late"):
    if exec_exit_price is not None:
        exit_price = exec_exit_price
    exit_marketable = True
```
When `exec_exit_price is None` (executable bid unresolved — thin/one-sided book as a losing binary converges, i.e. **exactly the ride-to-zero case**), a `updown_stop_loss` falls through with `exit_marketable=False` → placed **GTC** → rests → position rides to binary-zero. The logic is inverted: the missing-price case is the one that most needs to cross the bid NOW, not rest.

For `…befa91` the stop fired ~14 min before resolution, when a YES bid almost certainly still existed near ~0.45; a marketable FAK would have filled there (~−8%) instead of riding to −100%.

## Proposed fix A (primary — surgical)
Move `updown_stop_loss` into the same unconditional-marketable block as the other loss-cutting exits; stop gating marketable on `exec_exit_price is not None`.

```python
exit_marketable = False
# Loss-cutting / near-resolution exits MUST take the bid now (FAK), even when the
# executable bid price can't be resolved (thin/one-sided book as a losing binary
# converges) — that is exactly the ride-to-zero case. Set marketable UNCONDITIONALLY
# for these reasons; use exec_exit_price when available, else keep the current
# exit-side mark and let the FAK cross whatever bid exists. Gating marketable on
# exec_exit_price!=None inverted the logic: the missing-price case is the MOST urgent
# to cross, not rest as a GTC that never fills (rode 3 positions to $0 on 2026-07-27).
if reason in (
    "updown_stop_loss",
    "never_green_cut",
    "updown_flatten_pre_resolution",
    "updown_expired",
    "updown_time_stop",
    "take_profit_late",
):
    if exec_exit_price is not None:
        exit_price = exec_exit_price
    exit_marketable = True
```

**Scope/safety:** only flips GTC→FAK for exits already intended to be marketable. Does NOT touch entries, take_profit-on-winners (`take_profit`, not `take_profit_late`), hold-to-resolution winners, or sizing. Same order path — no double-sell. FAK SELL price rounding already crosses (ROUND_FLOOR for sell-marketable, clob_client.py:1467). Worst case a FAK eats thin-book slippage — strictly better than riding a loser to 0.

## Proposed fix B (secondary — defense-in-depth; Codex please weigh in)
Even forced-marketable exits showed a 1.1h-to-fill reconcile, and a truly one-sided book can't fill even a FAK. In `_handle_exit_decision` (main.py:4188–4226), when an exit is "accepted but not filled" and has been pending > one `exit_check_interval_sec`, **cancel and re-place FAK at the current best bid** (aggressive re-price) rather than polling a stale GTC to resolution. Risk: cancel/replace race → double-fill; needs the existing `pending_exit_order_id` guard to be airtight. Flag whether to include B now or ship A first.

## Questions for Codex
1. Any lane/reason where a stop SHOULD rest GTC (not cross)? (I believe none — a fired stop must exit.)
2. Fix B: safe to add cancel+re-place now, or ship A first and observe?
3. When `exec_exit_price is None`, is `exit_price` (current exit-side mark) a safe FAK limit, or should we pass an explicit cross-the-bid price / marketable-market order?
4. Does `reload_code.flag` re-import `live_testing.py` + `main.py`, or is a full restart required? (Assume full restart.)

## Deploy plan
Code change → Codex GO → back up `src/execution/live_testing.py` → apply → **restart required** (blocked until the 1 open position resolves + operator GO). No hot-reload for this.

---

## UPDATE — A+A+B all APPLIED (2026-07-27, operator chose complete fix)

Codex first pass = GO-with-nits on A. Operator scoped to the complete fix. Now applied across two files (staged; restart-class):

- **A** — `src/execution/live_testing.py:775`: `updown_stop_loss` moved into unconditional-marketable block. Backup `live_testing.py.bak_pre_exitmarketable_20260727_102016`.
- **A+** — `src/main.py` `_handle_exit_decision` place_order call: for live marketable (FAK) exits, override the LIMIT to an aggressive crossing price — SELL→`tick`, BUY→`1-tick` — so it takes the real best bid/ask instead of resting behind a stale mark. Fill still lands at the real book price; recorded P&L uses `exit_decision.unrealized_pnl` (not the limit), so no accounting corruption. Entries unaffected (they price their own FAK in place_entry_order). dry_run keeps the mark.
- **B-lite** — `src/main.py` pending-exit else branch: a marketable exit whose order is still `pending` AFTER `get_order_status` reconciles against trade history is a stale no-fill (FAK can't rest). Clear `pending_exit_order_id` and re-arm a FRESH FAK (NOT cancel+replace — prior FAK is already terminal, no double-fill). Bounded by `trading.exit_fak_max_retries` (default 40) per position; on cap, fall back to the old return (let resolution settle). Replaces the old unconditional `return` that blocked all re-exits (the befa91 ride-to-zero mechanism).

Both files `py_compile` clean. main.py working tree is far ahead of git HEAD (do NOT `git checkout`); revert = swap the two edited hunks back (surgical).

### Re-review asks for Codex
1. A+ aggressive pricing: is SELL→tick / BUY→(1-tick) correct for the CLOB match engine (fills at best opposing level, never worse)? Any tick/`_quantize_price_for_tick` interaction that could reject the order?
2. B-lite retry loop: cap=40 sane? Any path where re-arming a FAK could double-fill (e.g. a prior FAK that partially filled then reads pending)? Should we reconcile filled_size before re-arming?
3. `exit_fak_retries` never reset on a filled/closed position — is per-trade lifetime fine (positions are per-trade), or reset on FILL for safety?
4. Overall GO / NO-GO for restart-deploy.

---

## FINAL — Codex re-review resolved (2026-07-27)

- **A+ pricing VALID** (Codex): SELL→tick / BUY→(1-tick) survives `_quantize_price_for_tick` clamp to `[tick, 1-tick]` + SDK range check; no reject path.
- **B-lite NO-GO** (Codex): double-fill risk — `get_order_status` collapses PARTIAL→PENDING and trade-history recovery lacks filled quantity, so a full-size re-arm could over-sell/over-buy (BUY-side overbuy worst). **B-lite REVERTED** to the original `return`; left an inline note.
- **`exit_fak_retries` lifetime** was fine (positions per-trade) — moot now that B-lite is out.

### SHIPPING (Codex GO): A + A+
- A: `src/execution/live_testing.py` — updown_stop_loss unconditionally marketable.
- A+: `src/main.py` `_handle_exit_decision` — live marketable exits submit aggressive crossing limit (SELL→tick, BUY→1-tick); PnL unaffected (uses exit_decision.unrealized_pnl).
- Both `py_compile` clean. Restart-class; deploy on next clean restart (blocked until the 1 open position resolves + operator GO).

### DEFERRED follow-up: B-lite done right
Re-arm a stale-pending marketable exit ONLY after reconciling actual filled/remaining size (query remaining token holdings), and re-arm just the REMAINING size — never full size on ambiguous PENDING. Separate task + Codex pass before it ships.

---

## #49 CLOB ENTRY-OUTAGE FIX — Codex GO (2026-07-27)

Separate CRITICAL bug found while monitoring: bot went quiet ~76 min. NOT tape/gates — 152 HTTP 400 'invalid amounts, market buy orders maker amount max 2 decimals, taker max 4 decimals' since 15:48 UTC. Root cause: bot built ALL orders via create_order (LIMIT path); for tick 0.01 RoundConfig(price=2,size=2,amount=4) a BUY limit = maker(USDC) 4dec / taker(shares) 2dec, but Polymarket validates FAK/marketable as MARKET (maker<=2 / taker<=4) — precision SWAPPED -> reject.

FIX (clob_client.py place_order): route order_type in (FAK,FOK) -> client.create_market_order(MarketOrderArgs(amount, side, price, order_type)); amount=size*price for BUY (USDC), amount=size for SELL (shares). GTC unchanged (limit path). Import MarketOrderArgs added. Backup clob_client.py.bak_pre_marketorder_20260727_105215.

Codex GO-with-nits: BUY/SELL amount semantics correct; over-spend bounded; aggressive-exit-price interplay correct; post_order valid for market (post_only=False). NIT (non-blocking): a unit test still expects FAK->create_order — stale, update it (test cleanup, not runtime).

## RESTART BUNDLE (all Codex-GO, restart-class, 0 open positions NOW = clean window)
1. #49 clob_client.py — FAK/FOK -> market order (UNBLOCKS ALL ENTRIES). CRITICAL.
2. A  live_testing.py — updown_stop_loss unconditional marketable.
3. A+ main.py — aggressive FAK crossing price for live marketable exits.
LIVE already (no restart): eth 15m momentum-confirm (#2, config hot-reload).
Deferred: B-lite (#48), eth #1 size-damp, RSS tail-read.
Operator runs: .venv/bin/python src/main.py --live --confirm-live + YES.

---

## RIDE-TO-ZERO ROOT CAUSE FOUND + FIXED (2026-07-27, Codex GO-with-nits)

The A+/market-order fixes unblocked entries but losers STILL rode to zero. Traced it: not a one-sided book — a **killed-FAK-misreported-as-PENDING freeze**.
- A marketable FAK exit that is KILLED (no liquidity) leaves an EMPTY /data/order record + no trade; `_recover_status_from_trades` (clob_client.py ~1187) then returns **PENDING**.
- main.py saw PENDING, kept `pending_exit_order_id`, logged "still pending" and **returned every tick** → the position NEVER re-attempted its exit (66-76 dead re-emits) → rode to resolution.

### FIX (main.py _handle_exit_decision, PENDING else-branch) — Codex GO
Track `pos.exit_pending_ticks`; after GRACE (`trading.exit_fak_pending_grace_ticks`, default 3 ticks ~10-15s — long enough for a real fill to propagate to trade history and return FILLED above), a marketable **SELL** exit still reading PENDING = KILLED FAK → `cancel_order` (idempotent) + clear pending + re-arm fresh FAK this tick. Reset ticks on FILLED/re-arm.
DOUBLE-SELL-SAFE (Codex-confirmed): exits SELL the held token → venue caps fill at holdings; partial/real fills return FILLED (trade-match), not this path; gated SELL-only (non-SELL keeps old wait). Codex verified vs Polymarket docs (inventory/balance check bounds re-sell).

### Instrumentation (Option A, operator-approved, READ-ONLY) — clob_client.debug_stuck_order
Logs the raw CLOB order record (status/side/price/size_matched/type) + trade matches ONCE per stuck order → confirms killed-vs-resting live. Kept in the bundle.

### Codex nits (follow-ups, NOT blockers)
- Add focused unit test for the grace→cancel→re-arm path.
- Longer-term: order recovery should return matched SIZE so a partial exit reduces pos.size instead of "any trade = full close" (pre-existing under-count).

### RESTART BUNDLE v2 (all Codex-GO) — operator restarts to deploy
Already live in pid 3858 (10:58 restart): clob market-order entry fix, live_testing A, main.py A+, eth#2 config.
NEW since 3858 (need this restart): main.py killed-FAK SELL re-arm fix (STOPS ride-to-zero) + clob_client debug_stuck_order + test_exit_executable_stop FAK-routing test update.
Backups: clob_client.py.bak_pre_stuckdbg_20260727_133243, main.py.bak_pre_stuckdbg_20260727_133243.

---

## POSITION RECONCILER — phantom cleanup + manual-close detection (2026-07-27, Codex GO)

Operator found bot reported 6 open but Polymarket had 1 (5 phantoms). Root: unreliable CLOB fill-confirmation books phantom entries (recover-likely-fill) + strands exits at PENDING; AND the venue-reconciler was Olympus-only (main.py:2177 `if not using_olympus: return`) — never ported to CLOB.

FIX (Codex GO-with-nits after one NO-GO round):
- NEW clob_client.clob_open_condition_ids(): GET data-api.polymarket.com/positions?user=<funder>&sizeThreshold=1 (8s timeout), returns lowercased conditionIds with size>0, None on any error (fail-safe). Verified live.
- reconcile_open_positions_with_venue() un-gated to handle CLOB; drops journal positions absent from the venue set (phantom/manual-closed/resolved) from journal.open_positions + risk_manager.active_positions.
- HARDENING (Codex blocker fix): CONSECUTIVE-ABSENCE guard — drop only after position_reconcile_confirm_rounds (cfg, default 2, set 3 for live) consecutive successful+aged snapshots absent; present/young/unparseable resets streak. Prevents a spurious 200-[] Data-API glitch wiping real positions. AGE-GRACE (position_reconcile_grace_sec 120): never drop a fill younger than grace or with unparseable opened_at.
- PERIODIC: fast-exit loop calls reconciler every position_reconcile_interval_sec (120), AFTER exit checks (no exit latency).
- condition_id formats confirmed identical (0x-hex 66char, both lowercased). config keys added under trading:. Backups clob_client.py.bak_pre_reconciler_*, main.py.bak_pre_reconciler_*.

## FINAL RESTART BUNDLE (all Codex-GO) — operator restarts with LIVE_FRESH_SESSION=1
Bot STOPPED by operator. Restart: `LIVE_FRESH_SESSION=1 .venv/bin/python src/main.py --live --confirm-live` (fresh session avoids resuming phantoms; plain --live resumes+reloads them since the reconciler needs runtime to clear). Loads ALL: market-order entry fix, exit A/A+, killed-FAK SELL re-arm (ride-to-zero), debug_stuck_order, position reconciler, eth#2 (config).
