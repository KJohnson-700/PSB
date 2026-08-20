## Olympus Live Review

Branch: `live/olympus-smoke`  
Commit: `59b2fd1c579aaa80f131f9b078602e1d21b384a1`  
Scope: adversarial pre-LIVE review for `execution_provider=olympus`, real-money account, fee `$0`.

### One-Line Verdicts

| Task | Verdict | Reason |
|---|---:|---|
| RSI port | **GO** | The helper/call sites are scope-safe, asset attributes exist, fallback is fail-open to canonical RSI, and the new hard block only applies to `BUY_NO` when the pre-existing oversold RSI trigger has fired. |
| Olympus live config sanity | **NO-GO** | `OlympusClient` caches `dry_run=True` from YAML before `main()` applies `--live --confirm-live`, so live Olympus BUYs hit the smoke guard's dry-run `RuntimeError` and do not submit. |
| Residual CLOB contamination | **GO** | No live-path import of `fee_util`; `fee_util.py` is absent; `clob_client.py` is the 1707-line pre-CLOB/Olympus bridge and routes `execution_provider=olympus` through `OlympusClient.submit_trade`. |

## Findings

### NO-GO 1. Olympus smoke guard caches YAML `dry_run: true`, so `--live --confirm-live` still blocks every live order

**Files:** `config/settings.yaml`, `src/main.py`, `src/execution/clob_client.py`, `src/execution/olympus_client.py`

`config/settings.yaml` has `trading.dry_run: true` at `config/settings.yaml:50`. `main()` parses `--live --confirm-live` into `dry_run=False` at `src/main.py:6234`-`src/main.py:6242`, but `PolyBot()` is constructed first at `src/main.py:6340`; only after construction does `main()` mutate `bot.config["trading"]["dry_run"] = dry_run` at `src/main.py:6346`-`src/main.py:6347`.

During `PolyBot.__init__`, `self.clob_client = CLOBClient(self.config)` is built at `src/main.py:784`. `CLOBClient.__init__` immediately builds `OlympusClient(self._root_config)` at `src/execution/clob_client.py:187`-`src/execution/clob_client.py:190`. `OlympusClient.__init__` then caches `self._is_dry_run = bool(trading_cfg.get("dry_run", True))` at `src/execution/olympus_client.py:57`-`src/execution/olympus_client.py:73`.

The later live override updates the shared config dict, but it does **not** update `OlympusClient._is_dry_run`. The first live Olympus order reaches `CLOBClient.place_entry_order(... dry_run=False ...)` from `src/main.py:5740`-`src/main.py:5747`, then `CLOBClient.place_order` routes to Olympus at `src/execution/clob_client.py:718`-`src/execution/clob_client.py:735`. `OlympusClient.submit_trade` calls `_enforce_smoke_limits` at `src/execution/olympus_client.py:313`-`src/execution/olympus_client.py:326`, and `_enforce_smoke_limits` raises if `self._is_dry_run` is still true at `src/execution/olympus_client.py:114`-`src/execution/olympus_client.py:125`.

Impact: live mode starts, wallet checks can pass, signals can reach execution, but every smoke-mode Olympus order is blocked/failed with `"Olympus smoke-test guard called in dry_run (paper) mode"`. That violates the operator requirement that this actually trade live.

Required fix before live: apply the CLI live override before constructing `CLOBClient/OlympusClient`, or after `bot.config` mutation rebuild/update `bot.clob_client.olympus_client._is_dry_run` from the effective runtime config. Add a regression test that constructs with YAML `dry_run=True`, applies the live override, and verifies a smoke Olympus submit does not hit the dry-run guard.

### FIX-BEFORE-LIVE 2. Current smoke floor is incompatible with the reported `$1.03` Olympus cash balance

**Files:** `config/settings.yaml`, `src/execution/olympus_client.py`

`config/settings.yaml` sets `smoke_test.max_order_usd: 15`, `scale_live_sizing: true`, `scale_floor_usd: 5.0`, and `true_max_usd: 25` at `config/settings.yaml:441`-`config/settings.yaml:449`. `OlympusClient._smoke_scaled_buy_notional` scales every live BUY and floors it to at least `min(scale_floor_usd, max_order_usd)` at `src/execution/olympus_client.py:86`-`src/execution/olympus_client.py:93`.

If the already-probed Olympus balance is still `$1.03`, every live BUY will be submitted at **at least `$5`**, with no code-side cap against available cash before `POST /v1/trade`. That is likely to make Olympus reject all BUYs for insufficient funds. This is not a wrong-direction bug, but it is a live-readiness blocker if the goal is a real filled smoke trade.

Required fix before live: either fund the account above the configured smoke floor plus buffer, or set the smoke floor/cap to values the account can actually cover. Keep this as an ops/config correction, not a strategy gate change.

## RSI Port Review

Verdict: **GO**.

`_own_tf_rsi_macd` selects `tf_5m`/`tf_15m`/`tf_1h` and `macd_5m`/`macd_15m`/`macd_1h` by window at `src/strategies/sol_macro.py:2162`-`src/strategies/sol_macro.py:2182`. It only trusts own-timeframe RSI when the timeframe state exists and `price > 0` at `src/strategies/sol_macro.py:2171`-`src/strategies/sol_macro.py:2178`; otherwise it falls back to canonical `asset.rsi_14` at `src/strategies/sol_macro.py:2179`-`src/strategies/sol_macro.py:2181`. That fallback is direction-neutral in code: it only changes the RSI value fed to existing gate logic and cannot flip `action`.

The asset model supports these fields. `SOLAnalysis` defines `macd_1h`, `macd_15m`, `macd_5m`, `tf_5m`, `tf_15m`, and `tf_1h` at `src/analysis/sol_btc_service.py:128`-`src/analysis/sol_btc_service.py:167`; `TimeframeIndicatorState` has `price`, `rsi_14`, and `macd` at `src/analysis/sol_btc_service.py:116`-`src/analysis/sol_btc_service.py:124`. The service populates those fields at `src/analysis/sol_btc_service.py:798`-`src/analysis/sol_btc_service.py:899`. ETH uses the same service with `alt_symbol="ETHUSDT"` at `src/strategies/eth_macro.py:83`-`src/strategies/eth_macro.py:95`, so no ETH-specific override is required.

Scope is safe. In SOL, each loop assigns `is_updown` and `_updown_tf` before any RSI call at `src/strategies/sol_macro.py:3371`-`src/strategies/sol_macro.py:3378`; therefore the call at `src/strategies/sol_macro.py:4148` and the traditional-market call at `src/strategies/sol_macro.py:5031` cannot raise `NameError`. In ETH, `_updown_tf` and `is_updown` are assigned before the call at `src/strategies/eth_macro.py:983`-`src/strategies/eth_macro.py:989`, so `src/strategies/eth_macro.py:1517` is also scope-safe.

The exhaustion block is side-safe. `_resolve_rsi_gate` first requires the pre-existing RSI trigger to hit at `src/strategies/sol_macro.py:2196`-`src/strategies/sol_macro.py:2208`. The new hard block is guarded by `action == "BUY_NO"` at `src/strategies/sol_macro.py:2210`-`src/strategies/sol_macro.py:2221`; it never blocks `BUY_YES` and never mutates `action`. For non-oversold/non-overbought cases, behavior returns unchanged `(False, 0.0)` before the new block runs.

## Olympus Config And Execution Path

Verdict: **NO-GO** because of cached `dry_run`; otherwise the Olympus path is structurally wired.

`execution_provider: olympus` is set at `config/settings.yaml:40`; Olympus approval/smoke settings are present at `config/settings.yaml:434`-`config/settings.yaml:449`. `CLOBClient.using_olympus()` is based on `trading.execution_provider` cached at construction at `src/execution/clob_client.py:187`-`src/execution/clob_client.py:209`, and live entries are routed to `OlympusClient.submit_trade` at `src/execution/clob_client.py:718`-`src/execution/clob_client.py:735`.

`OLYMPUS_API_KEY` sourcing is correct: `load_project_dotenv(...)` runs before bot construction at `src/main.py:6250`-`src/main.py:6254`; `OlympusClient` reads `os.getenv("OLYMPUS_API_KEY")` or config `api_key` at `src/execution/olympus_client.py:40`-`src/execution/olympus_client.py:45`; `main()` also passes `OLYMPUS_API_KEY` through `bot.set_api_keys` at `src/main.py:6350`-`src/main.py:6369`, which calls `set_olympus_credentials` at `src/main.py:1986`-`src/main.py:1989`.

Config hot-reload does not silently revert `dry_run` after startup. `_HOT_RELOAD_TRADING_KEYS` at `src/main.py:108`-`src/main.py:118` excludes `dry_run`, and `_select_hot_reload_config` only copies whitelisted trading keys at `src/main.py:132`-`src/main.py:149`. That part is safe.

## Residual CLOB Contamination

Verdict: **GO**.

`src/strategies/fee_util.py` is absent, and current `src/strategies/sol_macro.py` / `src/strategies/eth_macro.py` do not import it. The only `execution_fees` live-code hits are config and exit-fee accounting in `src/execution/live_testing.py`; config has `execution_fees.enabled: false` at `config/settings.yaml:431`-`config/settings.yaml:433`.

`src/execution/clob_client.py` is 1707 lines, matching the expected pre-CLOB-size file. It contains `FAK` support in the legacy direct-CLOB path, but under Olympus the branch exits earlier through `self.using_olympus()` at `src/execution/clob_client.py:718`-`src/execution/clob_client.py:775`. I found no `create_market_order`, no `fee_util` import, and no CLOB position-adoption reconciler in `src/main.py`, `src/strategies`, or the active execution path. The `data-api.polymarket.com/positions` reference at `src/execution/clob_client.py:1474`-`src/execution/clob_client.py:1485` is a dead-returning CLOB-side stub, not an Olympus live path.

## Verification Run

`py_compile` passed for:

- `src/strategies/sol_macro.py`
- `src/strategies/eth_macro.py`
- `src/execution/olympus_client.py`
- `src/execution/clob_client.py`
- `src/main.py`

`tests/test_olympus_client.py` passed: `12 passed in 2.26s`.

## Overall

**FIX-BEFORE-LIVE**

1. Fix the cached `OlympusClient._is_dry_run` startup-order bug before any real-money run.
2. Confirm Olympus cash is above the configured `$5` smoke floor, or adjust smoke sizing to the actual funded balance before expecting a filled BUY.

