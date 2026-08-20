## Second Opinion: SDK Migration And RSI Exhaustion

### Bottom Line

**Topic 1 SDK migration:** **GO for a proof spike, NO-GO for migration commitment.** The PyPI package exists, but the claimed API is only partially true, and deposit-wallet signing must be proven with this exact funder before any live adapter work matters.

**Topic 2 RSI fix:** **Ship only if you accept that it is a narrow bounce filter, not a complete oversold-short fix.** The implementation matches the stated mechanics, but several nearby RSI paths still use canonical `asset.rsi_14`, and the “still falling” exception can keep entering the late down-leg.

---

## Topic 1: `polymarket-client` Migration

### 1. Does `polymarket-client==0.2.0` exist and expose the claimed API?

**Installed tree verdict: UNVERIFIED.** The project venv does **not** have `polymarket-client` installed:

- `polymarket_client`: missing
- `polymarket`: missing
- `polymarket-client`: not installed
- current installed CLOB packages are `py-clob-client 0.34.6` and `py-clob-client-v2 1.0.1`

**PyPI/wheel verdict: package exists, claimed API partially matches, not drop-in.** `pip index` lists `polymarket-client (0.2.0)`, and the downloaded wheel exports `AsyncSecureClient` from `polymarket`, not `polymarket_client`: `wheel:polymarket/__init__.py:3`, `wheel:polymarket/clients/__init__.py:4`.

The claimed constructor shape exists:

- `AsyncSecureClient.create(private_key=..., wallet=...)` exists at `wheel:polymarket/clients/async_secure.py:335`.
- `wallet` defaults to the signer’s deposit wallet per docstring at `wheel:polymarket/clients/async_secure.py:349`.

The claimed market-order API is close but **not exactly the stated signature**:

- `place_market_order(... side="BUY", amount=..., max_spend=..., max_price=..., order_type="FAK")` exists at `wheel:polymarket/clients/async_secure.py:2208`.
- For `SELL`, it takes `shares=...`, not `amount=...`, at `wheel:polymarket/clients/async_secure.py:2220`.
- Default `order_type` is `"FAK"`, not `"FOK"`, at `wheel:polymarket/clients/async_secure.py:2216` and `wheel:polymarket/clients/async_secure.py:2240`.

Unified CLOB + Gamma + Data + relayer is real at a high level: exported models include CLOB/Data/Gamma objects in `wheel:polymarket/__init__.py:20`, public/secure clients expose CLOB reads and data positions/trades in the wheel, and relayer/deposit-wallet code exists. But this is **not proof of behavioral compatibility** with the current adapter.

### 2. Is signature type 3 / deposit wallet supported?

**Likely yes in the SDK code, but go/no-go still depends on exact wallet derivation.**

The new wheel explicitly maps `DEPOSIT_WALLET` to signature type `3` at `wheel:polymarket/_internal/wallet.py:18` and `wheel:polymarket/_internal/wallet.py:20`. Market-order drafts set `funder_address=ctx.wallet` and `signer=ctx.signer.address`, which is the right conceptual split for a deposit wallet: `wheel:polymarket/_internal/actions/orders/market.py:132`.

The risk: the SDK classifies the wallet only if it equals one of its deterministic addresses for the private-key signer. Unknown wallets raise at `wheel:polymarket/_internal/wallet.py:136`. It also auto-resolves legacy UUPS vs beacon deposit wallets when `wallet` is omitted at `wheel:polymarket/clients/async_secure.py:3264`, then deploys/validates a default deposit wallet in `_ensure_wallet_ready` at `wheel:polymarket/clients/async_secure.py:2448`.

**Where to prove it in the spike:** create the client with the exact live pair:

```python
client = await AsyncSecureClient.create(
    private_key=POLYMARKET_PRIVATE_KEY,
    wallet="0x7d81246BbE1e91f84f5A791D56fb1865545D78A9",
)
assert client.wallet_type == "DEPOSIT_WALLET"
assert int(signature_type_for(client.wallet_type)) == 3
assert str(client.wallet).lower() == "0x7d81246bbE1e91f84f5a791d56fb1865545d78a9".lower()
```

If that fails, the migration is dead for this account. Do this before reading books, placing paper orders, or building the adapter.

### 3. Is the strangler adapter behind `CLOBClient` the right seam?

**Yes, with one caveat: keep `CLOBClient` async even if the SDK is native async.** `main.py` already awaits the adapter surface:

- Credentials and user WS still depend on adapter state: `src/main.py:798`, `src/main.py:2253`, `src/main.py:2279`, `src/main.py:2524`.
- Public reads are awaited through adapter methods: `src/main.py:2701`, `src/main.py:3572`, `src/main.py:4147`, `src/main.py:4213`, `src/main.py:4299`, `src/main.py:4377`, `src/main.py:5913`.
- Execution/cancel/status paths are awaited through adapter methods: `src/main.py:4634`, `src/main.py:4707`, `src/main.py:4764`, `src/main.py:6380`, `src/main.py:6736`.

The current SDK calls are isolated in `src/execution/clob_client.py`: imports at `src/execution/clob_client.py:25`, live construction at `src/execution/clob_client.py:535`, order creation/posting at `src/execution/clob_client.py:1231`, cancel at `src/execution/clob_client.py:1567`, trades at `src/execution/clob_client.py:1675`, and read-only client at `src/execution/clob_client.py:1956`.

So removing `run_in_executor` should **not** leak into scanner/strategy/main call sites if the adapter preserves the same async methods and normalized return types.

### 4. Smallest safe step / one decisive experiment

The smallest decisive experiment is **not** a broad read-only/paper adapter. It is:

**Auth + wallet classification + signed dry market-order construction for the exact deposit wallet, without posting.**

Why this is more decisive: if `AsyncSecureClient.create(private_key, wallet=funder)` cannot classify wallet type as `DEPOSIT_WALLET`, derive credentials, and `create_market_order` cannot produce a signed order whose `signature_type == 3` and `maker/funder == funder`, then no read-only adapter or paper plumbing matters.

After that passes, do the tiny FOK/FAK smoke. But wallet signing is the first kill-switch test.

### 5. Migration risks the prior audit missed or underweighted

- **Order response is no longer a dict.** Current code requires `post_order` response to be dict-like and reads `order_id/orderID/id/orderId` at `src/execution/clob_client.py:1277`. New SDK returns `AcceptedOrder | RejectedOrder` with `ok`, `order_id`, `status`, `making_amount`, `taking_amount`, or `code/message`: `wheel:polymarket/models/clob/order_response.py:61` and `wheel:polymarket/models/clob/order_response.py:98`.
- **FAK/FOK killed orders may be clean rejections, not exceptions.** New SDK encodes `fak_not_filled`/`fok_not_filled` in `RejectedOrder` at `wheel:polymarket/models/clob/order_response.py:15`. Existing code has a lot of recovery logic around “post errored but filled” and killed-FAK status ambiguity: `src/execution/clob_client.py:1825`, `src/main.py:4682`.
- **BUY/SELL amount semantics differ.** Current adapter computes BUY market amount as USDC and SELL market amount as shares at `src/execution/clob_client.py:1220`. New SDK formalizes this split as BUY `amount`, SELL `shares`: `wheel:polymarket/clients/async_secure.py:2211`, `wheel:polymarket/clients/async_secure.py:2223`.
- **User-channel fill path is a migration dependency, not optional.** `main.py` wires user events to `apply_user_fill_event` at `src/main.py:798`; fill accounting parses maker/taker/nested IDs and matched size at `src/execution/clob_client.py:573`. New SDK stream payload models may not match those field names.
- **Credential re-derivation changes shape.** Current code relies on py-clob-client retaining the L1 signer and exposing `create_or_derive_api_key` plus `set_api_creds`: `src/execution/clob_client.py:711`. New SDK creates with `credentials` or derives during `AsyncSecureClient.create`: `wheel:polymarket/clients/async_secure.py:349`; adapter must preserve `get_ws_creds()` for `src/main.py:798`.
- **Allowance/balance recovery semantics move into SDK.** Current code calls `get_balance_allowance` explicitly at `src/execution/clob_client.py:827`; new SDK has `post_order_with_allowance_recovery` behind `place_market_order` at `wheel:polymarket/clients/async_secure.py:2260`. That can hide side effects and relayer approvals unless logged.
- **Public read method signatures are keyword-only.** Existing adapter calls `pc.get_order_book(token_id)` and `pc.get_midpoint(tid)` at `src/execution/clob_client.py:1970` and `src/execution/clob_client.py:2079`. New clients use `get_order_book(*, token_id=...)` / `get_midpoint(*, token_id=...)` in the wheel.
- **Liquidity bug remains separate unless explicitly fixed.** Current strategy liquidity checks bypass the floor when `market.liquidity == 0`: `src/strategies/sol_macro.py:4068`, `src/strategies/eth_macro.py:1265`, and scanner builds `market.liquidity` from Gamma at `src/market/scanner.py:1165`. Migrating the SDK alone does not automatically replace those checks with executable depth.

### 6. Topic 1 one-line answer

**GO on the wallet-auth/signed-order spike now; NO-GO on a read-only/paper adapter until signature type 3 is proven with the exact funder.**

---

## Topic 2: RSI “Paper Not Live” And Oversold-Short Bleed

### 1. Does implementation match the described fix, and is it correct?

**Mechanically yes. Behaviorally only partly.**

The helper does map window to own TF and uses the populated-price check:

- `{"5m": "tf_5m", "15m": "tf_15m", "1h": "tf_1h"}` at `src/strategies/sol_macro.py:2227`.
- It trusts own-TF RSI only when `price > 0.0`: `src/strategies/sol_macro.py:2236`.
- It falls back to canonical `asset.rsi_14`: `src/strategies/sol_macro.py:2238`.
- It maps MACD to `macd_5m/macd_15m/macd_1h`: `src/strategies/sol_macro.py:2240`.

The MACD attribute names are valid. `MACDResult` carries `histogram`, `histogram_rising`, and `crossover`; the TF state carries `.macd`, while the asset carries `macd_5m/macd_15m/macd_1h`: `src/analysis/sol_btc_service.py:123`, `src/analysis/sol_btc_service.py:143`, `src/analysis/sol_btc_service.py:146`, `src/analysis/sol_btc_service.py:858`.

The exhaustion logic matches the description:

- RSI hit for `BUY_NO` is `rsi <= sell_floor`: `src/strategies/sol_macro.py:2263`.
- Still-falling is `hist < 0`, `not histogram_rising`, and not `BULLISH_CROSS`: `src/strategies/sol_macro.py:2276`.
- It blocks only when not still falling: `src/strategies/sol_macro.py:2280`.
- If still falling, it proceeds to hard/soft policy; with current config `rsi_hard_gate_enabled=false` and `rsi_soft_penalty_buy_no=0.0`, that means it allows the short: `config/settings.yaml:1064`, `config/settings.yaml:1067`.

The fallback can still defeat the fix during feed gaps. Empty TF state defaults `price=0.0`, `rsi_14=50.0`: `src/analysis/sol_btc_service.py:116`. The new helper correctly avoids the fake 50, but fallback to canonical RSI is **15m for SOL-family**, not 4h: `src/analysis/sol_btc_service.py:826`. For BTC, canonical RSI is 4h: `src/analysis/btc_price_service.py:1138`.

### 2. Is “RSI set for paper not live” real?

**No evidence.** I found no dry-run/live conditional RSI thresholds or indicator logic.

The only dry-run strategy override found is ETH BTC HTF bias override: `src/strategies/eth_macro.py:661`. The live/paper branch skips CLOB credentials in paper mode at `src/main.py:2259`, and execution differs in `src/execution/clob_client.py:1381`, but that is order routing, not RSI or indicators.

Config RSI values are strategy config, not dry-run/live split: e.g. `config/settings.yaml:1064`, `config/settings.yaml:1067`, `config/settings.yaml:1299`, `config/settings.yaml:1302`.

### 3. Is the exhaustion gate the right lever?

**It is a plausible bounce filter, but it can perpetuate shorting the late down-leg.**

The symptom is “shorts into oversold RSI right before a bounce.” This fix blocks once own-TF MACD is rising/flat/bull-crossing, which targets the immediate pre-bounce state. But it explicitly allows fresh oversold shorts while MACD is still falling at `src/strategies/sol_macro.py:2279`. In a fast 5m/15m market, “still falling” can be the exact late chase that becomes the next bounce.

The stricter alternative would be “block fresh oversold shorts, then allow re-entry only after momentum turns and price confirms continuation.” That would be a first-principles restriction and would reduce trade count, so it conflicts with the current calibration mandate. For calibration, the built fix is acceptable only if ghost/live logs tag these cases separately: **oversold + still_falling admitted** vs **oversold + exhaustion blocked**. Without that split, you will not know whether the allowed bucket is still the bleed.

### 4. Other assets/paths with same wrong-TF-RSI bug?

**Covered by inheritance:** XRP, HYPE, BNB, and DOGE inherit `SolMacroStrategy`, so their main `_resolve_rsi_gate` calls get the helper automatically: `src/strategies/xrp_macro.py:34`, `src/strategies/hype_macro.py:44`, `src/strategies/bnb_macro.py:52`, `src/strategies/doge_macro.py:50`.

**ETH main gate is covered** because ETH inherits the helper and calls it at `src/strategies/eth_macro.py:1576`.

**Not fully covered:** nearby RSI logic still reads canonical RSI:

- SOL-family buy-no LTF override uses `sol.rsi_14`, not own TF: `src/strategies/sol_macro.py:1167`.
- SOL-family pocket RSI floor/ceiling uses `sol.rsi_14`: `src/strategies/sol_macro.py:3628`, `src/strategies/sol_macro.py:3657`.
- SOL-family tape freshness passes canonical `sol.rsi_14`: `src/strategies/sol_macro.py:5864`.
- ETH pocket gates and RSI floors use `eth.rsi_14`: `src/strategies/eth_macro.py:1523`, `src/strategies/eth_macro.py:1555`, `src/strategies/eth_macro.py:2067`, `src/strategies/eth_macro.py:2398`.
- Bitcoin uses own-TF RSI for bias voting at `src/strategies/bitcoin.py:1430`, `src/strategies/bitcoin.py:1444`, `src/strategies/bitcoin.py:1458`, but many skip records and freshness logic still log/use `ta.rsi_14`, which is 4h: `src/strategies/bitcoin.py:2528`, `src/strategies/bitcoin.py:3491`.

The 1h paths are covered for the new helper via `tf_1h` mapping at `src/strategies/sol_macro.py:2227`, but fresh-cross override uses faster-lead RSI for 1h (`tf_15m`) by design at `src/strategies/sol_macro.py:5063` and `src/strategies/eth_macro.py:2017`.

### 5. Bottom line for the three findings

**Finding A: inert hard/soft RSI gate.** **Correct diagnosis; fix incomplete.** `_resolve_rsi_gate` now has a hard exhaustion block independent of `rsi_hard_gate_enabled`, but if the short is “still falling,” current config still applies no hard block and no soft penalty. Safe to ship as calibration instrumentation, not as a complete bleed fix.

**Finding B: wrong timeframe RSI.** **Mostly correct for the main RSI gate; incomplete globally.** `_own_tf_rsi_macd` fixes the two main call sites in SOL-family and ETH at `src/strategies/sol_macro.py:4298`, `src/strategies/sol_macro.py:5181`, and `src/strategies/eth_macro.py:1576`. Other RSI gates still read canonical RSI. Safe to ship, but do not claim the whole strategy is own-TF clean.

**Finding C: trend-following side selection without exhaustion check.** **Partly correct, partly risky.** The exhaustion branch blocks bounce-state oversold shorts, but still admits oversold shorts when MACD is falling. Safe to ship for calibration because it does not broadly choke frequency, but the admitted bucket must be labeled and reviewed.

---

## Final Decision

**SDK:** GO only for exact-wallet auth/signed-order spike. Do not build a full read-only/paper strangler until `wallet_type == DEPOSIT_WALLET`, signature type `3`, funder address, L2 creds, and unsigned/signed market-order shape are proven.

**RSI:** Ship in next restart bundle if you want a narrow bounce filter and more labeled data. Do not present it as a complete paper-vs-live fix; no dry-run/live RSI divergence was found.

## Metadata / Summary

**Tags:** #PSB #Polymarket #CLOB #SDKMigration #RSI #LiveTrading #Calibration

**Related Concepts:** [[CLOBClient Adapter]], [[Deposit Wallet Signature Type 3]], [[Oversold Short Exhaustion]], [[Own-Timeframe RSI]], [[Ghost Log Calibration]]

**Summary:** `polymarket-client==0.2.0` exists on PyPI and appears to support deposit-wallet signature type 3, but the current environment does not install it and the API is not drop-in. The RSI fix is mechanically implemented for the main SOL/ETH gate, but it remains incomplete across nearby canonical-RSI paths and still allows oversold shorts while MACD is falling.
