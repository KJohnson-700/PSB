## Alt Lane Policy Audit — 2026-06-12

### Scope

Audit request: validate the proposed alt-lane actions before giving Claude an execution diff.

Sources:

- `config/settings.yaml`
- `data/calibration/rejected_candidates_settled.jsonl`
- `data/calibration/trades_settled.jsonl`
- `scripts/lane_decision_sheet.py`

Important limitation: settled ghosts validate admission-side rejects only. They do not model live exits, stop behavior, take-profit behavior, slippage, or time decay. The repo's `lane_decision_sheet.py` is therefore the safer action filter because it adjusts raw ghost EV by observed taken-trade exit delta.

### Live Policy Map

All reviewed alt strategies are currently enabled:

| Strategy | 15m LONG | 15m SHORT | 1h LONG | 1h SHORT |
|---|---:|---:|---:|---:|
| `sol_macro` | min_edge `0.09`, band `0.42-0.58`, size `1.0` | `0.06`, `0.42-0.58`, `1.0` | `0.07`, `0.42-0.58`, `0.3` | `0.07`, `0.42-0.58`, `0.3` |
| `eth_macro` | `0.11`, `0.42-0.55`, `1.0` | `0.10`, `0.42-0.58`, `1.0` | `0.09`, `0.42-0.58`, `0.3` | `0.09`, `0.42-0.58`, `0.3` |
| `hype_macro` | `0.06`, `0.42-0.58`, `1.0` | `0.06`, `0.42-0.58`, `1.0` | `0.06`, `0.42-0.58`, `0.3` | `0.06`, `0.42-0.58`, `0.3` |
| `xrp_macro` | `0.04`, `0.42-0.57`, `1.0` | `0.06`, `0.42-0.55`, `1.0` | `0.05`, `0.42-0.58`, `0.3` | `0.06`, `0.42-0.55`, `0.3` |
| `doge_macro` | `0.085`, `0.42-0.55`, `1.0` | `0.06`, `0.42-0.55`, `1.0` | `0.04`, `0.42-0.58`, `0.3` | `0.06`, `0.42-0.58`, `0.3` |
| `bnb_macro` | `0.50`, `0.42-0.57`, `1.0` | `0.50`, `0.42-0.55`, `1.0` | `0.08`, `0.42-0.58`, `0.3` | `0.08`, `0.42-0.58`, `0.3` |

Takeaway: the live bot is not using a `0.15` entry floor for these alt up/down lanes. Most lane floors are already `0.42-0.47`, and 1h lanes are broadly throttled by `size_multiplier: 0.3`.

### Claim Check — Raw Ghost vs Live-Band Ghost

`BUY_YES` uses YES price inside `[entry_price_min, entry_price_max]`. `BUY_NO` uses YES price above `entry_price_min` and below the configured minimum NO-price guard.

| Proposed KEEP lane | Raw ghost EV | Live-band ghost EV | Audit read |
|---|---:|---:|---|
| `eth_macro 15m BUY_NO` | `+0.060`, n `26700` | `+0.060`, n `26406` | Good admission edge, but exit-adjusted sheet says `NO-GO`. Do not blindly unleash. |
| `hype_macro 1h BUY_NO` | `+0.062`, n `6774` | `+0.074`, n `6016` | Raw admission supports shadow/forward-test. Taken n is only `6`; not enough exit proof. |
| `bnb_macro 1h BUY_YES` | `+0.181`, n `1994` | `+0.184`, n `1369` | Strongest raw 1h candidate. Taken n is only `6`; shadow or small-size forward-test before full unlock. |
| `bnb_macro 15m BUY_NO` | `+0.016`, n `9917` | `+0.028`, n `8055` | Marginal raw edge; exit-adjusted sheet says `NO-GO`. Also live config currently disables BNB 15m via `min_edge: 0.50`. |
| `doge_macro 1h BUY_YES` | `+0.100`, n `1931` | `+0.113`, n `1397` | Strong raw candidate, but taken n is only `3`; shadow/forward-test. |
| `hype_macro 15m BUY_YES` | `-0.001`, n `25378` | `+0.004`, n `23118` | Too thin. Exit-adjusted sheet says `NO-GO`. |
| `xrp_macro 1h BUY_NO` | `-0.021`, n `1295` | `+0.022`, n `507` | Live-band filter flips slightly positive, but exit-adjusted sheet says `NO-GO`. |
| `sol_macro 1h BUY_YES` | `+0.014`, n `2183` | `-0.040`, n `1537` | Pasted KEEP does not survive live-band filter. |
| `sol_macro 1h BUY_NO` | `-0.045`, n `2570` | `-0.007`, n `1573` | Not a keep. |
| `doge_macro 15m BUY_YES` | `+0.003`, n `29426` | `+0.003`, n `27240` | Thin raw edge and unstable live exit behavior. Shadow only. |

### Claim Check — Proposed KILL Lanes

The KILL list is mostly supported.

| Proposed KILL lane | Raw ghost EV | Live-band ghost EV | Audit read |
|---|---:|---:|---|
| `sol_macro 15m BUY_YES` | `-0.022` | `-0.043` | Kill supported. |
| `sol_macro 15m BUY_NO` | `-0.052` | `-0.046` | Kill supported. |
| `eth_macro 15m BUY_YES` | `-0.077` | `-0.110` | Kill supported. |
| `eth_macro 1h BUY_NO` | `-0.134` | `-0.136` | Raw kill supported, but exit-adjusted sheet says `GO` due positive exit delta. Needs careful review before disabling. |
| `xrp_macro 15m BUY_YES` | `-0.048` | `-0.048` | Kill supported. |
| `xrp_macro 15m BUY_NO` | `-0.037` | `-0.035` | Kill supported. |
| `xrp_macro 1h BUY_YES` | `-0.070` | `-0.129` | Kill supported. |
| `bnb_macro 15m BUY_YES` | `-0.049` | `-0.056` | Kill supported. |
| `bnb_macro 1h BUY_NO` | `-0.092` | `-0.098` | Kill supported. |
| `doge_macro 15m BUY_NO` | `-0.068` | `-0.064` | Kill supported. |

### Starvation Diagnosis

Top 1h raw candidates and likely blockers:

| Lane | Raw ghost | Live-band ghost | Top reject reasons | Likely source |
|---|---:|---:|---|---|
| `bnb_macro 1h BUY_YES` | WR `65.6%`, EV `+0.181` | WR `59.5%`, EV `+0.184` | `ai_none_marginal_threshold`, `lane_min_edge`, `ai_call_limit_marginal_threshold` | AI marginal path plus min-edge; live size throttled to `0.3x`. |
| `doge_macro 1h BUY_YES` | WR `62.1%`, EV `+0.100` | WR `55.9%`, EV `+0.113` | `ai_none_marginal_threshold`, `lane_min_edge`, `oracle_basis_block` | AI marginal path plus min-edge/oracle basis; live size throttled to `0.3x`. |
| `hype_macro 1h BUY_NO` | WR `56.2%`, EV `+0.062` | WR `53.6%`, EV `+0.074` | `lane_entry_window`, `lane_min_edge`, `liquidity` | 1h entry window and liquidity/min-edge; live size throttled to `0.3x`. |

The starvation lever is not broad 4H/15m MACD gate removal. It is mostly per-lane admission plumbing: AI marginal rejects, min-edge rejects, entry-window rejects, liquidity rejects, plus `0.3x` 1h sizing.

### Safer Execution Brief

Recommended Claude instructions:

1. Do not apply global `entry_price_min: 0.50`.
2. Do not widen to `entry_price_max: 0.85`; live alt bands are much tighter and the `0.50-0.85` test is not live-equivalent.
3. Keep KILL lanes disabled or effectively disabled, except review `eth_macro 1h BUY_NO` separately because live-exit-adjusted evidence conflicts with raw ghost evidence.
4. For 1h starved candidates, do a narrow forward-test:
   - `bnb_macro 1h BUY_YES`
   - `doge_macro 1h BUY_YES`
   - `hype_macro 1h BUY_NO`
5. For those three lanes only, consider raising `size_multiplier` from `0.3` to `0.5` or reducing the specific marginal admission reject path, but do not remove alt gates wholesale.
6. Keep `eth_macro 15m BUY_NO` under review, not full unleash, because raw ghost is positive but exit-adjusted decision sheet says `NO-GO`.
7. Keep `doge_macro 15m BUY_YES` as shadow/observer only; raw edge is only `+0.003` and live stability is poor.

### Metadata/Summary

Tags: #PSB #GhostLab #AltLanes #LaneEntryPolicy #QuantExecution

Related Concepts: [[Ghost Log Validation]], [[Lane Decision Sheet]], [[Alt Macro Strategies]], [[BUY_YES BUY_NO Semantics]]

Summary: The alt audit supports killing most dead 15m/1h lanes, but pushes back on a blanket entry floor change and broad gate removal. The best next execution target is a narrow 1h forward-test on `bnb_macro BUY_YES`, `doge_macro BUY_YES`, and `hype_macro BUY_NO`, because those have strong raw ghost admission edge but too little taken-trade exit evidence for full-size deployment.
