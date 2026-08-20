# Olympus Smoke Compare

## Scope

Read-only investigation against current working tree on `live/olympus-jul29` (`HEAD` = `96eaaaf`, working tree dirty). No bot/runtime commands were run.

Original states found:

- **Original isolated Olympus smoke scripts/results:** commit `fdf987a` (`untracked files on exp/lane-edge-reallocation`, dated 2026-07-10, carrying June 13 untracked smoke scripts) contains `scripts/olympus_live_smoke_buy.py`, `scripts/olympus_live_smoke_sell.py`, and `scripts/olympus_smoke_status.py`. Runtime results exist at `data/runtime/olympus_smoke_last.json` and `data/runtime/olympus_smoke_close_last.json`.
- **Original committed live Olympus calibration config:** `3f07f12` (`chore(live): wire live Olympus calibration run (~$6/trade, kill switch on)`, 2026-06-13).
- **Original completed Olympus live bundle:** `6d8d2ce` (`feat(live): startup position-sync + bankroll = real Olympus equity`, 2026-06-13).
- **Related direct CLOB smoke, not Olympus broker smoke:** `c779e44` adds `scripts/direct_clob_smoke.py`, explicitly for direct CLOB V2 plumbing, not Olympus.

## Original Olympus Smoke State

### Isolated one-order Olympus smoke

The actual one-order Olympus smoke script is in `fdf987a:scripts/olympus_live_smoke_buy.py`.

- It loads only `OLYMPUS_API_KEY` from env or repo `.env` and does not print keys/wallets/full trade IDs (`fdf987a:scripts/olympus_live_smoke_buy.py:39`, `:54`).
- It selects either an explicit slug or a current BTC 5m market, checks order-book executability, computes `size = amount_usd / max_price`, then calls `OlympusClient.build_trade_payload()` (`fdf987a:scripts/olympus_live_smoke_buy.py:60`, `:70`, `:76`, `:82`, `:91`, `:92`).
- It creates an in-script Olympus config with `live_order_approved: true`, `smoke_test.enabled: true`, `max_order_usd = args.amount_usd`, `max_orders_per_run: 1`, and requires `marketSlug`/`conditionId` (`fdf987a:scripts/olympus_live_smoke_buy.py:108`, `:113`, `:114`, `:116`, `:117`, `:118`, `:119`).
- It submits once via `client.submit_trade(payload)`, polls status, and writes `data/runtime/olympus_smoke_last.json` (`fdf987a:scripts/olympus_live_smoke_buy.py:131`, `:136`, `:145`, `:150`, `:158`, `:175`).

The close script sells only the saved smoke BUY token:

- It reads `data/runtime/olympus_smoke_last.json`, verifies `kind == olympus_smoke_buy`, fetches BUY status, derives filled shares from status or portfolio, checks bid liquidity, and blocks close if bid is more than `max_loss_cents` below buy price unless `--allow-large-loss` is set (`fdf987a:scripts/olympus_live_smoke_sell.py:79`, `:81`, `:106`, `:110`, `:114`, `:119`, `:126`, `:127`).
- It builds a SELL payload from the saved BUY market identifiers and writes `data/runtime/olympus_smoke_close_last.json` (`fdf987a:scripts/olympus_live_smoke_sell.py:140`, `:150`, `:164`, `:169`, `:175`, `:191`).

Smoke result evidence:

- `data/runtime/olympus_smoke_last.json` records `created_at=2026-06-13T22:18:24Z`, `kind=olympus_smoke_buy`, `status=SUCCEEDED`, selected slug `btc-updown-5m-1781388900`, `amount_usd=1.0`, and payload `amountUsd=1.0`, `maxPrice=0.99`.
- `data/runtime/olympus_smoke_close_last.json` records `created_at=2026-06-13T22:21:08Z`, `kind=olympus_smoke_sell`, `status=SUCCEEDED`, `sharesNormalized=3.448274`, `minPrice=0.01`.

### Original committed live Olympus calibration config

`3f07f12:config/settings.yaml` is the original committed Olympus run state:

- `trading.execution_provider: olympus`, `entry_mode: marketable`, `dry_run: true` with live entered by CLI `--live` (`3f07f12:config/settings.yaml:47`, `:56`, `:63`).
- Sizing pinned around `$6/trade`: `default_position_size: 6`, `max_position_size: 6`, `max_exposure_per_trade: 0.08` (`3f07f12:config/settings.yaml:81`, `:82`, `:85`).
- Exposure tiers all pinned to 6: `full_size=6`, `moderate_size=6`, `minimal_size=6`; min floors `6/6/6`; kill switch on (`3f07f12:config/settings.yaml:2100`, `:2103`, `:2104`, `:2105`, `:2107`, `:2108`, `:2109`, `:2110`).
- Olympus config: `live_order_approved: true`, `smoke_test.enabled: false`, `smoke_test.max_order_usd: 5`, `max_orders_per_run: 1`, requires slug/condition id (`3f07f12:config/settings.yaml:372`, `:377`, `:378`, `:379`, `:380`, `:381`, `:382`, `:383`).

Important nuance: the `$6/trade` bot run did **not** use `olympus.smoke_test.enabled`; it made strategy sizing small enough upstream. The Olympus smoke cap was available as code/config but disabled for the calibration run.

Runtime journal evidence from that era:

- `data/paper_trades/test_20260613_220516/entries.jsonl` has an Olympus `sol_macro` entry with `olympus_status=SUCCEEDED`, `olympus_spent_usd=5.999999`, and `olympus_requested_amount_usd=6.0`.
- `data/paper_trades/test_20260613_221216/entries.jsonl` has Olympus entries for `eth_macro` and `bitcoin` with `olympus_spent_usd=5.999999` and `olympus_requested_amount_usd=6.0`.
- `data/logs/polybot_20260613.log` confirms Olympus routing and entries, including `trading.execution_provider=olympus — skipping direct CLOB credential init`, `Olympus trade queued: side=BUY provider=olympus`, and journal entries around 2026-06-13 22:07-22:46 local log time.

## Original Execution/Journaling/Reconciliation

In `6d8d2ce`, the Olympus adapter had a hard cap but no proportional smoke scaling:

- `OlympusClient.__init__` reads `smoke_test.enabled`, `max_order_usd`, `max_orders_per_run`, and market-id requirements (`6d8d2ce:src/execution/olympus_client.py:40`, `:48`, `:49`, `:50`, `:51`, `:52`).
- `_enforce_smoke_limits()` blocks after the per-run count, requires `conditionId`/`marketSlug`, calculates BUY notional from payload `amountUsd`, and raises if over cap (`6d8d2ce:src/execution/olympus_client.py:73`, `:76`, `:81`, `:83`, `:86`, `:88`, `:103`).
- `submit_trade()` checks live approval, enforces smoke limits, POSTs `/v1/trade`, increments submitted-order count only after a trade id (`6d8d2ce:src/execution/olympus_client.py:263`, `:264`, `:269`, `:271`, `:277`).

The CLOB client routes live orders to Olympus when configured:

- `CLOBClient` derives `_execution_provider` and constructs `OlympusClient` (`6d8d2ce:src/execution/clob_client.py:164`, `:167`).
- `using_olympus()` returns true only for `execution_provider == "olympus"` (`6d8d2ce:src/execution/clob_client.py:185`).
- In `place_order()`, dry-run paper short-circuits before Olympus; live Olympus builds payload and calls `submit_trade()` (`6d8d2ce:src/execution/clob_client.py:665`, `:695`, `:701`, `:712`).
- `place_entry_order()` makes paper a conservative FAK fill and live `marketable` a FAK-style `place_order()`; Olympus ignores maker semantics because the config uses `entry_mode: marketable` (`6d8d2ce:src/execution/clob_client.py:859`, `:912`, `:924`, `:925`).

Journaling/reconciliation:

- `_update_olympus_order_execution()` stores execution fields, status history, and overwrites order price/filled size from real Olympus status (`6d8d2ce:src/execution/clob_client.py:326`, `:334`, `:341`, `:346`, `:348`, `:350`).
- `main.py` records actual filled share count, not requested size, before constructing the position and journal entry (`6d8d2ce:src/main.py:3611`, `:3618`, `:3621`, `:3631`, `:3664`, `:3672`, `:3673`).
- `refresh_live_wallet_bankroll()` uses account value, and `reconcile_open_positions_with_venue()` drops phantoms only after a successful Olympus portfolio fetch (`6d8d2ce:src/main.py:1377`, `:1382`, `:1404`, `:1414`, `:1418`, `:1427`).

## Current State on `live/olympus-jul29`

### Config

Current working-tree `config/settings.yaml`:

- `execution_provider: olympus`, `entry_mode: marketable`, `dry_run: true` (`config/settings.yaml:54`, `:55`, `:103`).
- Sizing now has `default_position_size: 6`, `max_position_size: 25`, `max_exposure_per_trade: 0.08` (`config/settings.yaml:123`, `:124`, `:127`).
- Olympus smoke is now enabled with a larger/run-cap/scaling band: `live_order_approved: true`, `await_fill_on_submit: true`, `smoke_test.enabled: true`, `max_order_usd: 15`, `max_orders_per_run: 40`, `scale_live_sizing: true`, `scale_floor_usd: 5.0`, `true_max_usd: 25` (`config/settings.yaml:492`, `:493`, `:496`, `:497`, `:498`, `:499`, `:502`, `:503`, `:504`).
- Exposure tiers restored to wider normal sizing: `full_size=25`, `moderate_size=15`, `minimal_size=8`; floors `15/15/5`; loss kill switch remains true (`config/settings.yaml:2384`, `:2385`, `:2386`, `:2387`, `:2389`, `:2390`, `:2391`, `:2392`).

### Current Olympus path

- Current `OlympusClient` adds smoke-band scaling fields: `scale_live_sizing`, `scale_floor_usd`, `true_max_usd`, plus an `_is_dry_run` guard (`src/execution/olympus_client.py:54`, `:59`, `:60`, `:64`, `:74`).
- `_smoke_scaled_buy_notional()` maps BUY notional from `[0, true_max_usd]` to `[0, max_order_usd]`, floors at `scale_floor_usd`, and caps at `max_order_usd` (`src/execution/olympus_client.py:77`, `:87`, `:91`, `:92`, `:93`, `:94`).
- `_enforce_smoke_limits()` now fails if called while cached dry-run is true, then applies the same cap/order-count/slug/condition checks (`src/execution/olympus_client.py:115`, `:121`, `:127`, `:132`, `:134`, `:137`, `:154`).
- `submit_trade()` now scales BUY `amountUsd` before enforcement and logs both requested/submitted notional (`src/execution/olympus_client.py:374`, `:380`, `:384`, `:386`, `:387`, `:389`, `:390`, `:395`, `:405`, `:406`).
- Current `CLOBClient` still short-circuits dry-run before Olympus and uses the Olympus branch for live orders (`src/execution/clob_client.py:1015`, `:1045`, `:1051`, `:1062`).
- Current `main.py` explicitly propagates runtime `--live` dry-run state into OlympusClient after CLI confirmation, fixing the cached-dry-run block risk (`src/main.py:7413`, `:7423`, `:7425`, `:7437`).

### Current journaling/reconciliation

- Current `CLOBClient._update_olympus_order_execution()` still writes Olympus fill fields and uses venue-filled price/size as truth (`src/execution/clob_client.py:410`, `:418`, `:425`, `:430`, `:432`, `:434`).
- Current `main.py` adds a stronger live Olympus phantom guard: if an Olympus entry is accepted but not terminally `FILLED`, it is not journaled as active (`src/main.py:6402`, `:6411`, `:6415`, `:6416`, `:6427`).
- Current `main.py` still journals actual filled shares/price, and subscribes held positions to WS after adding them (`src/main.py:6429`, `:6436`, `:6440`, `:6442`, `:6479`, `:6480`, `:6483`, `:6491`, `:6492`).
- Current reconciliation is venue-agnostic, but for Olympus still fetches Olympus open condition IDs and fails safe if fetch fails (`src/main.py:2373`, `:2382`, `:2387`, `:2388`, `:2391`).

## Same vs Drifted

| Area | Original | Current | Same or drifted | Behavioral impact |
|---|---:|---:|---|---|
| Execution provider | `olympus` (`3f07f12:config/settings.yaml:47`) | `olympus` (`config/settings.yaml:54`) | Same | Same broker path. |
| Entry mode | `marketable` (`3f07f12:config/settings.yaml:56`) | `marketable` (`config/settings.yaml:55`) | Same | Same intended fill policy. |
| Live approval | true (`3f07f12:config/settings.yaml:377`) | true (`config/settings.yaml:492`) | Same | Live order permission still armed. |
| Olympus smoke enabled | false (`3f07f12:config/settings.yaml:379`) | true (`config/settings.yaml:497`) | Drifted | Current relies on broker smoke guard; original `$6` run relied on upstream small sizing. |
| Smoke order cap | `$5` disabled (`3f07f12:config/settings.yaml:380`) | `$15` enabled (`config/settings.yaml:498`) | Drifted | Current single submitted BUY can be up to `$15`, not `$6`; still below `true_max_usd=25`. |
| Max orders per run | 1 disabled (`3f07f12:config/settings.yaml:381`) | 40 enabled (`config/settings.yaml:499`) | Drifted | Current smoke is a multi-trade run, not a one-order smoke. |
| Upstream max position | `$6` (`3f07f12:config/settings.yaml:82`) | `$25` (`config/settings.yaml:124`) | Drifted | Current lets Kelly express larger true size upstream. |
| Exposure tier sizes | `6/6/6` (`3f07f12:config/settings.yaml:2103`) | `25/15/8` (`config/settings.yaml:2385`) | Drifted | Current lane/exposure bands are normal-size, then smoke-scaled at broker BUY submit. |
| Exposure floors | `6/6/6` (`3f07f12:config/settings.yaml:2107`) | `15/15/5` (`config/settings.yaml:2389`) | Drifted | Current upstream floors can produce >$6 true requested notional. |
| Loss kill switch | true (`3f07f12:config/settings.yaml:2110`) | true (`config/settings.yaml:2392`) | Same | Same per-lane loss-streak safety remains. |
| Fill wait | defaults to smoke enabled, but smoke disabled in config (`6d8d2ce:src/execution/clob_client.py:172`) | explicit `await_fill_on_submit: true` (`config/settings.yaml:493`) | Safer drift | Current more explicitly waits for terminal fill before journaling. |
| Smoke cap semantics | hard block only (`6d8d2ce:src/execution/olympus_client.py:103`) | proportional BUY scaling, then hard block (`src/execution/olympus_client.py:380`, `:387`, `:390`) | Drifted/safety improvement | Fixes the old problem where cap could suppress all Kelly-sized live entries. |
| Dry-run guard | none in Olympus guard | guard plus runtime propagation (`src/execution/olympus_client.py:121`; `src/main.py:7425`) | Safer drift with dependency | Safe only if launch path reaches `src/main.py:7413`; direct external use of `OlympusClient` with YAML `dry_run: true` and smoke enabled would block. |
| Journal filled size | actual venue fill (`6d8d2ce:src/main.py:3611`) | actual venue fill (`src/main.py:6429`) | Same | Preserves original orphan-prevention behavior. |
| Non-terminal journaling | order could remain pending unless await finished | explicit no-journal unless `FILLED` on live Olympus (`src/main.py:6402`) | Safer drift | Reduces phantom positions. |

## Lane Config Drift

The current branch does **not** reproduce the June 13 lane configuration. It is the Jul-29 strategy/lane bundle run over Olympus.

Examples:

- Bitcoin `use_ai_updown` changed from `false` to `true`; AI decision timeout changed `90 -> 40`; simple 1h long `entry_min` changed `0.50 -> 0.55`; 5m up `min_edge` changed `0.08 -> 0.05` (`3f07f12:config/settings.yaml:482`, `:485`, `:499`, `:501`, `:526`; current `config/settings.yaml:634`, `:637`, `:640`, `:642`, `:667`).
- SOL `max_ai_calls_per_scan` changed `5 -> 10`, `ai_decision_timeout_sec` `40 -> 10`, while main edge/windows stay broadly similar (`3f07f12:config/settings.yaml:722`, `:724`, `:739`; current `config/settings.yaml:891`, `:893`, `:908`).
- ETH materially loosened some center/LTF settings: `min_edge_15m_when_ltf_unconfirmed 0.10 -> 0.06`, `min_edge_when_centered 0.12 -> 0.025`, and allows exchange when oracle missing (`3f07f12:config/settings.yaml:908`, `:932`; current `config/settings.yaml:1104`, `:1128`, `:1139`).
- HYPE was enabled in original and is disabled currently (`3f07f12:config/settings.yaml:1127`; current `config/settings.yaml:1346`).
- XRP/BTC/alt AI call budget and timeout generally changed from `5/6 calls` and `40s` to `10 calls` and `10s` for several lanes (`3f07f12:config/settings.yaml:1368`, `:1370`, `:1595`, `:1597`; current `config/settings.yaml:1617`, `:1619`, `:1875`, `:1877`).

## Verdict

The current `live/olympus-jul29` bundle **does not faithfully reproduce** the original June 13 Olympus smoke/calibration state. It **does preserve and improve the Olympus execution/journaling safety path**, but it intentionally runs a different strategy/lane/sizing bundle.

Ranked behavioral differences:

1. **Sizing model changed from upstream hard `$6` to true `$25` upstream plus broker smoke scaling.** Original committed live run pinned `max_position_size` and exposure tiers to `$6`; current upstream sizing can compute `$25`, then Olympus scales BUY notional by `15/25 = 0.6`, floors at `$5`, caps at `$15` (`config/settings.yaml:124`, `:498`, `:502`, `:503`, `:504`; `src/execution/olympus_client.py:91`). This is the biggest difference. It appears to be a safe improvement over a hard cap that would break Kelly by rejecting >cap orders, but it will not produce identical `$6` live order sizes.
2. **Smoke mode changed from one-order/manual or disabled-run guard to multi-order enabled run.** Original isolated script allowed one `$1` smoke BUY and one saved close; original bot run had `smoke_test.enabled: false`. Current smoke allows up to 40 submitted orders per process at up to `$15` each (`config/settings.yaml:497`, `:498`, `:499`).
3. **Lane strategy behavior changed materially.** Current branch includes Jul-29 lane gates, AI settings, HYPE disabled, ETH loosenings, scanner/warmup/slippage settings, and normal exposure tiers. This can change which signals trade even if Olympus execution is working.
4. **Execution safety improved.** Current code scales live BUY notional before cap enforcement, logs requested/submitted notional, blocks smoke guard in dry-run, propagates runtime `--live` state, waits for fills, and refuses to journal non-terminal Olympus entries (`src/execution/olympus_client.py:380`, `:395`; `src/main.py:7413`, `:6427`).
5. **Journaling/reconciliation remains aligned or safer.** Both original and current journal actual filled shares/price and reconcile against Olympus portfolio; current adds phantom protection and venue-agnostic reconciliation.

Bottom line: **Current is not an exact smoke-state reproduction.** It is closer to “Jul-29 strategy bundle with Olympus broker and live smoke-band safety.” The main thing that could behave differently from the original successful smoke is not the Olympus API plumbing; it is the combination of larger upstream Kelly/exposure, proportional `$15` broker scaling, 40-order smoke run cap, and changed lane gates. If the operator wants exact June 13 behavior, the repo would need `$6` upstream sizing and original lane config; if the goal is safe current live testing, the current smoke-scaling is the right structural fix for the historical “smoke cap broke Kelly” failure mode.

## Metadata/Summary

Tags: #PSB #Olympus #SmokeTest #ExecutionPath #KellySizing #LiveTrading
Related Concepts: [[OlympusClient]], [[CLOBClient]], [[Kelly Sizing]], [[Trade Journal]], [[Position Reconciliation]], [[Smoke Test]]

Summary: The original Olympus smoke had a one-order `$1` script result and a later committed `$6/trade` Olympus calibration run with smoke guard disabled. The current branch keeps the Olympus execution path safer, but it is behaviorally different because it runs Jul-29 lanes with `$25` upstream sizing and live BUY smoke scaling to a `$5-$15` broker band.
