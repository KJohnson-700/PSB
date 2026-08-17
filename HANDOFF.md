## Claude Handoff — 2026-08-17 PSB Chop-Bleed Wiring Audit

### Immediate Context

The operator asked for an audit of the losing strategies in the current paper run because the bot started strong and then bled sideways. Treat the active run as a **split session**:

- `data/paper_trades/test_20260817_011504`
- `data/paper_trades/test_20260817_025250`

Do **not** review only the newer session. A restart split the journal, and the combined run is the real operational picture.

### What Codex Changed

#### 1. BTC 15m cheap-NO floor

File: `config/settings.yaml`

Added:

```yaml
strategies:
  bitcoin:
    buy_no_min_no_price_15m: 0.40
```

Reason:

- Combined split-session losses showed `bitcoin|15m|down` was the worst confirmed closed lane:
  - `n=2`
  - `WR=0%`
  - `PnL=-$25.33`
- Both losses were `BUY_NO` at cheap NO prices:
  - `YES=0.805`, NO about `0.20`, expired red for `-$12.71`
  - `YES=0.715`, NO about `0.29`, expired red for `-$12.62`
- The journaled `entry_policy` for `bitcoin|15m|down` showed a `0.45-0.55` band, but BTC had no `buy_no_min_no_price_15m`, so the final lane-price check fell back to the generic cheap-NO floor `0.20`.
- ETH/XRP/DOGE already had this class of cheap-NO floor; BTC 15m did not.

This is a **narrow wiring/config fix**, not a blanket cut of `bitcoin|15m|down`.

Expected behavior:

- Blocks BTC 15m down entries where NO is below `0.40`.
- Keeps near-centered BTC 15m down entries eligible, e.g. `YES=0.595` gives NO `0.405` and is not blocked by this floor.

#### 2. BNB duplicate key cleanup

File: `config/settings.yaml`

Removed a duplicate `bnb_macro.disable_buy_yes_15m` key. Both copies were `true`, so this is source-of-truth cleanup only, not a behavior change.

#### 3. Prior same-session triage already present

These were already applied during this incident and should not be casually reverted:

- `trading.exit_rules.hold_fixed_take_profit` enabled with `threshold_pct: 0.40`.
- Fixed-TP lane list includes:
  - `eth_macro|15m|down`
  - `xrp_macro|5m|down`
  - `eth_macro|5m|down`
  - `sol_macro|1h|down`
  - `xrp_macro|1h|down`
  - `bitcoin|1h|down`
  - `doge_macro|5m|up`
  - `xrp_macro|5m|up`
  - `bnb_macro|15m|down`
  - `eth_macro|1h|up`
- `sol_macro.alt_1h_simple_long.enabled: false`
- `sol_macro.marginal_ev_admit_lanes: []`
- `sol_macro.admit_marginal_on_quant_sides: SHORT`
- `bnb_macro.marginal_ev_admit_lanes: ["15m:LONG"]`
- `bnb_macro.admit_marginal_on_quant_sides: SHORT`

Purpose: remove below-min 1h LONG rescues and bank specific green giveback lanes while the exit layer is being re-evaluated.

### Evidence From The Split Session

Closed-lane recompute after the audit:

| Lane | Closed | PnL | WR |
|---|---:|---:|---:|
| `bitcoin|15m|down` | 2 | `-$25.33` | `0.00` |
| `eth_macro|5m|up` | 3 | `-$16.87` | `0.33` |
| `bnb_macro|5m|down` | 1 | `-$15.64` | `0.00` |
| `doge_macro|5m|up` | 5 | `-$14.96` | `0.60` |
| `xrp_macro|1h|down` | 2 | `-$8.27` | `0.50` |
| `bnb_macro|1h|up` | 2 | `-$3.62` | `0.50` |
| `bnb_macro|15m|down` | 2 | `+$8.52` | `1.00` |
| `eth_macro|5m|down` | 2 | `+$11.30` | `1.00` |
| `hype_macro|1h|up` | 1 | `+$14.96` | `1.00` |

Important interpretation:

- BTC 15m down had a confirmed config/wiring gap.
- ETH/DOGE 5m losses are not confirmed crossed-wiring from this sample. They may be strategy/exit economics issues.
- BNB `5m|up` looked directionally crossed in the journal because the reason text retained pre-flip `side=SHORT`, but the later `macd5m_momentum_flip` intentionally executed `BUY_YES`. Treat that as telemetry debt unless future rows show final action and final side truly disagree.

### What Not To Undo

- Do not revert `buy_no_min_no_price_15m: 0.40` unless clean live data shows BTC 15m down at NO `<0.40` is positive after current exit fixes.
- Do not re-enable SOL/BNB broad `admit_marginal_on_quant_sides: both`; that was the below-min 1h leak.
- Do not infer `eth_macro|5m|down` should be data-cut from recent bugged/favorite-policy data. Prior audit found the clean-native sample was not cut-ready.
- Do not treat old per-lane `updown_stop_loss_pct` values as active under current `hold_all: true`; current resolver sets up/down stop loss to `0.0`.
- Do not call this a regime-only issue until journaled code/config changes are separated from tape effects.

### Validation Already Run

Commands:

```bash
.venv/bin/python -m pytest \
  tests/test_bitcoin.py::test_bitcoin_15m_buy_no_quant_guard_reads_explicit_15m_keys \
  tests/test_bitcoin.py::test_bitcoin_direction_resolver_suppresses_quant_flip_without_momentum \
  tests/test_live_exit_overrides.py::test_hold_fixed_take_profit_banks_configured_hold_lane \
  -q
```

Result:

```text
3 passed
```

Additional checks:

- YAML parses.
- Duplicate YAML key scan passed.
- `strategies.bitcoin.buy_no_min_no_price_15m` resolves to `0.40`.
- The new floor blocks the two observed BTC 15m losses and does not block centered NO prices.

### Files Updated By Codex In This Incident

- `config/settings.yaml`
- `src/execution/live_testing.py`
- `tests/test_live_exit_overrides.py`
- `docs/AGENT_CHANGELOG.md`
- `HANDOFF.md`
- Second Brain note:
  - `/Users/mainfolder/Documents/Hermes Second Brain/psb/hourly-briefs/2026-08-16/2026-08-17-chop-bleed-triage.md`

### Next Claude Task

1. Combine both session folders when reviewing current performance.
2. Verify no new BTC 15m down entries occur with NO `<0.40`.
3. Review whether fixed TP is clipping 5m winners too small:
   - compare `hold_fixed_take_profit` wins against subsequent resolution outcome and MFE.
   - especially `doge_macro|5m|up`, `eth_macro|5m|up`, and `xrp_macro|5m|up`.
4. Clean up telemetry debt:
   - final journal reason should include final side/action after flip.
   - avoid stale pre-flip `side=SHORT` text surviving into executed `BUY_YES` rows.
5. Do not add new gates before proving whether the current loss is entry-side direction, exit economics, or clipped winners.

## Claude Handoff — Alt 15m/1h Volatility Capture Simplification

### Objective

Implement the operator's actual thesis: 15m and 1h alt up/down markets should be simplified into intrawindow volatility-capture strategies.

The current bot is over-coded and over-gated. The goal is to cut down the indicator stack and make longer-timeframe markets about capturing a percentage of normal volatility, not proving a full held-to-resolution directional forecast.

In a 1h market, even random YES/NO entries can often see meaningful percentage movement before resolution. Each 1h market also follows the prior just-ending window, giving an immediate first signal about the asset's current direction. The bot should use that simple structure before layering on more indicators.

Primary audit artifact:

- `docs/session_reports/alt_lane_policy_audit_20260612.md`

Use strategy/validation rules from:

- `CLAUDE.md`
- `docs/STRATEGY_ENTRY_SPEC.md`
- `AGENTS.md`

### Correction From Codex

Codex previously framed the handoff as blocking losing lanes to protect bankroll. That was too narrow and too held-to-resolution oriented.

The operator's goal is:

- use 15m/1h time available after window open
- reduce over-coded gates
- compare current asset direction against the previous just-ended window and current window start price
- capture a percentage of intrawindow volatility
- actively exit into repricing/profit before resolution
- keep collecting data on weak lanes instead of turning them completely dark

Do not convert this request into blanket lane killing unless the lane is being moved to explicit **shadow/small-size data collection** with logging intact.

### Do Not Do

- Do **not** apply a global `entry_price_min: 0.50`.
- Do **not** widen alt `entry_price_max` to `0.85`.
- Do **not** remove 4H/15m MACD or BTC-context gates wholesale for alts.
- Do **not** rely only on raw held-to-resolution ghost EV.
- Do **not** fully disable data-collection lanes without replacing them with shadow logging or small-size controlled execution.
- Do **not** solve over-gating by adding another large indicator stack.
- Do **not** make "hold to resolution" the default strategy language for 15m/1h alt lanes.

### Why Raw Ghosts Are Incomplete Here

Settled ghosts answer: "Would this rejected entry have won if held to resolution?"

The operator's thesis is different: "Can the bot capture a percentage move inside the 15m/1h window and exit before resolution?"

Therefore:

- raw ghost WR/EV can understate lanes where active exits harvest moves before resolution
- raw ghost WR/EV can overstate lanes where exits ruin held winners
- the needed evidence is intrawindow excursion: max favorable move, time-to-profit, drawdown before profit, and exit quality
- consider removing or sharply reducing "hold to resolution" language from these strategy paths and docs, except where explicitly describing ghost validation limitations

Look for existing code/logs before adding new abstractions:

- `src/execution/exit_excursion_shadow.py`
- `data/calibration/exit_excursion_shadow.jsonl`
- `src/execution/updown_exit_shared.py`
- `src/analysis/window_delta.py`
- `data/calibration/window_delta_shadow.jsonl`
- `data/calibration/trades_settled.jsonl`

## Simplify The Signal

### Required Feature: Window Anchors

For every 15m/1h up/down candidate, record:

- the asset price at the start of the current Polymarket window
- the final direction of the previous just-ended window
- the current market YES/NO price

At scan time compute:

- `previous_window_direction`
- `previous_window_return_pct`
- `window_start_price`
- `current_oracle_price`
- `move_from_start_pct`
- `minutes_since_window_start`
- `minutes_to_resolution`
- `direction_from_start`: `UP`, `DOWN`, or `FLAT`
- `simple_direction_confirmed`: previous-window direction and current move support the candidate side
- `momentum_speed_pct_per_min`

Use the correct asset oracle/exchange feed for each strategy:

- `eth_macro`: ETH oracle/exchange basis policy
- `sol_macro`: SOL oracle/exchange basis policy
- `hype_macro`: HYPE/Hyperliquid service
- `xrp_macro`: XRP oracle/exchange basis policy
- `doge_macro`: DOGE oracle/exchange basis policy
- `bnb_macro`: BNB oracle/exchange basis policy

Do not use BTC price as the primary start anchor for alt payoffs. BTC context can remain secondary context only.

### Entry Rule Shape

For 15m and 1h alt lanes, add a config-gated simple volatility-capture path. This path should bypass or soften much of the existing over-gated indicator stack when the setup is clean.

Recommended initial config shape:

```yaml
intrawindow_volatility_capture:
  enabled: true
  shadow_only: true
  simplify_gates: true
  use_previous_window_direction: true
  use_current_window_start_anchor: true
  min_move_from_start_pct_15m: 0.08
  min_move_from_start_pct_1h: 0.15
  min_minutes_since_start_15m: 2.0
  min_minutes_since_start_1h: 5.0
  target_profit_pct: 0.10
  hard_profit_take_pct: 0.20
  stale_momentum_exit_min: 8.0
  max_entry_price_yes: 0.80
  min_entry_price_yes: 0.20
  require_oracle_basis_ok: true
  require_market_repricing_lag: true
  disabled_gates_for_clean_setup:
    - ai_marginal_gate
    - excessive_htf_confirmation
    - btc_secondary_context_gate
    - redundant_macd_stack
```

Interpretation:

- For `BUY_YES`, the previous/current direction should support UP and market YES should not already be fully repriced.
- For `BUY_NO`, the previous/current direction should support DOWN and market NO should not already be fully repriced.
- If market price has already moved too far, log as `momentum_confirmed_but_late`.
- If asset moved but Polymarket has not repriced, that is the target entry condition.
- Keep only safety filters that prevent bad fills, stale oracle/basis mistakes, or risk-limit violations.

### Exit Rule Shape

The point is **not** to hold every entry to resolution.

Add or verify profit-taking logic for these volatility-capture entries:

- take profit when the market reprices enough to capture the move
- partial or full exit at `target_profit_pct`
- hard exit at `hard_profit_take_pct`
- time-stop if momentum stalls
- exit if price crosses back toward start anchor
- exit when the trade thesis is no longer about intrawindow volatility and has degraded into resolution gambling
- preserve current global risk limits and Kelly sizing

Initial shadow metrics to log for every candidate:

- `entry_yes_price`
- `entry_no_price`
- `best_yes_price_after_entry`
- `best_no_price_after_entry`
- `max_favorable_pct`
- `max_adverse_pct`
- `time_to_5pct_profit_sec`
- `time_to_10pct_profit_sec`
- `time_to_20pct_profit_sec`
- `crossed_back_to_start`
- `would_exit_profit`
- `would_exit_reason`

### Gate Reduction Requirement

Before adding indicators, Claude should list the existing gates that currently block 15m/1h alt entries and classify them:

- **Safety:** liquidity, stale oracle, basis mismatch, duplicate exposure, risk limit
- **Signal:** MACD, RSI, AI marginal, BTC context, HTF confirmation, LTF confirmation
- **Execution:** price band, entry window, slippage, spread

For the volatility-capture path:

- keep Safety gates
- keep necessary Execution gates
- remove or soften redundant Signal gates
- make BTC context informational unless explicitly proven useful for that asset/lane
- avoid requiring multiple indicators to agree before a clean intrawindow move can be traded

## Lane Treatment

Do not hard-kill these lanes only because held-to-resolution ghost EV is negative. Reclassify them into execution modes:

- `execute`: live/paper execution allowed
- `small_size`: execution allowed at reduced `size_multiplier`
- `shadow`: no execution, full candidate and excursion logging
- `blocked`: no execution only if instrumentation already proved no intrawindow edge

### High-Priority Volatility-Capture Test Lanes

These should be the first lanes to implement/verify with simplified intrawindow volatility capture:

| Strategy | Window | Side | Action | Why |
|---|---:|---|---|---|
| `bnb_macro` | `1h` | LONG | `BUY_YES` | Strong raw ghost admission edge; starved by marginal/size paths. |
| `doge_macro` | `1h` | LONG | `BUY_YES` | Strong raw ghost admission edge; starved and likely suited to intrawindow repricing. |
| `hype_macro` | `1h` | SHORT | `BUY_NO` | Strong raw ghost admission edge; entry-window/liquidity issues need targeted handling. |
| `eth_macro` | `15m` | SHORT | `BUY_NO` | Large raw positive admission pool; do not hold blindly, but this is exactly where active exits may matter. |

### Risky Lanes To Keep As Shadow Or Small-Size, Not Dark

These looked poor under held-to-resolution ghost/live-band evidence, but keep instrumentation on so the volatility-capture thesis can be tested:

| Strategy | Window | Side | Action | Mode |
|---|---:|---|---|---|
| `sol_macro` | `15m` | LONG | `BUY_YES` | `shadow` until intrawindow excursion proves edge |
| `sol_macro` | `15m` | SHORT | `BUY_NO` | `shadow` until intrawindow excursion proves edge |
| `eth_macro` | `15m` | LONG | `BUY_YES` | `shadow` or `small_size` only |
| `xrp_macro` | `15m` | LONG | `BUY_YES` | `shadow` |
| `xrp_macro` | `15m` | SHORT | `BUY_NO` | `shadow` |
| `xrp_macro` | `1h` | LONG | `BUY_YES` | `shadow` |
| `bnb_macro` | `15m` | LONG | `BUY_YES` | `shadow` |
| `bnb_macro` | `1h` | SHORT | `BUY_NO` | `shadow` |
| `doge_macro` | `15m` | SHORT | `BUY_NO` | `shadow` |
| `bnb_macro` | `15m` | SHORT | `BUY_NO` | keep effectively disabled for execution, but log shadow momentum |
| `hype_macro` | `15m` | LONG | `BUY_YES` | `shadow` or `small_size` only |
| `xrp_macro` | `1h` | SHORT | `BUY_NO` | `shadow` until excursion says otherwise |
| `sol_macro` | `1h` | LONG | `BUY_YES` | `shadow` until live-band/intrawindow evidence improves |
| `sol_macro` | `1h` | SHORT | `BUY_NO` | `shadow` |
| `doge_macro` | `15m` | LONG | `BUY_YES` | `shadow` or `small_size`; raw edge is thin but volume is large |

### Special Review

| Strategy | Window | Side | Action | Reason |
|---|---:|---|---|---|
| `eth_macro` | `1h` | SHORT | `BUY_NO` | Raw held ghost says bad, but `scripts/lane_decision_sheet.py --since 2026-05-15 --min-taken 15` says `GO` due positive observed exit delta. Inspect taken trades before changing. |
| `eth_macro` | `15m` | SHORT | `BUY_NO` | Raw admission is positive, exit-adjusted sheet says `NO-GO`; this is a prime candidate for simplified volatility capture plus active profit exits. |

## Config / Implementation Guidance

Prefer adding the simplified volatility path behind explicit config flags, not replacing every existing path globally.

Likely locations:

- shared alt strategy path in `src/strategies/sol_macro.py` if inherited/reused by other macro strategies
- strategy-specific overrides in `src/strategies/eth_macro.py`, `hype_macro.py`, `xrp_macro.py`, `doge_macro.py`, `bnb_macro.py`
- exit helpers in `src/execution/updown_exit_shared.py`
- excursion logging in `src/execution/exit_excursion_shadow.py`
- config in `config/settings.yaml`

Keep the current price bands for normal entries. The intrawindow volatility path can have its own price controls because it is a different setup.

Also search strategy docs/comments for "hold to resolution" language. Do not delete references needed to explain ghost validation, but remove or rewrite language that implies holding to resolution is the intended live exit model for 15m/1h alt trades.

## Tests / Verification

Run targeted tests first:

```bash
.venv/bin/python -m pytest tests/test_lane_entry_policy.py tests/test_bnb_macro.py tests/test_doge_macro.py tests/test_hype_macro.py tests/test_xrp_macro.py tests/test_sol_macro.py tests/test_eth_macro.py
```

Add tests for the new volatility-capture resolver:

- start anchor above/below current price maps to correct side
- previous window direction can seed the first signal
- `BUY_YES` requires upward move from start
- `BUY_NO` requires downward move from start
- late repriced markets are skipped/logged
- shadow mode logs without executing
- small-size mode preserves risk limits
- basis/oracle stale state blocks execution but still logs diagnostics
- clean volatility setup bypasses/softens redundant signal gates while preserving safety gates

Then run:

```bash
.venv/bin/python -m pytest tests/test_strategy_enabled_defaults.py tests/test_live_config_apply.py tests/test_strategy_execution_drivers.py tests/test_updown_exit_shared.py
```

## Strategy Log Updates

Because this is a material strategy change, update the strategy log after editing:

- `projects/polymarket-bot/strategy-log/sol_macro.md`
- `projects/polymarket-bot/strategy-log/eth_macro.md`
- `projects/polymarket-bot/strategy-log/hype_macro.md`
- `projects/polymarket-bot/strategy-log/xrp_macro.md`
- `projects/polymarket-bot/strategy-log/doge_macro.md`
- `projects/polymarket-bot/strategy-log/bnb_macro.md`

Read `projects/polymarket-bot/strategy-log/_index.md` first and use its exact template. Actual outcome stays `pending` until at least 15 closed trades since the change.

Also append a short index note to:

- `docs/AGENT_CHANGELOG.md`

## Verification After Restart

After local restart, inspect dashboard/logs for:

- start-window anchor fields present on 15m/1h alt candidates
- previous-window direction present on 15m/1h alt candidates
- shadow rows emitted for risky lanes instead of going dark
- volatility-capture entries clearly differentiated from normal entries
- profit exits tagged separately from held-to-resolution outcomes
- docs/comments no longer imply hold-to-resolution is the normal alt 15m/1h objective
- no global entry-band drift
- no broad alt-gate removal

Use dashboard/Ghost Lab, live journal, `exit_excursion_shadow.jsonl`, and local logs. Do not use removed backtest engines as proof.

## Metadata/Summary

Tags: #PSB #ClaudeHandoff #AltLanes #VolatilityCapture #WindowStartAnchor #GateReduction #GhostLab

Related Concepts: [[Ghost Log Validation]], [[Lane Decision Sheet]], [[Intrawindow Volatility]], [[Start Window Momentum]], [[Alt Macro Strategies]], [[BUY_YES BUY_NO Semantics]]

Summary: This handoff corrects the goal from "block losing lanes" to "simplify over-gated 15m/1h alt logic and capture intrawindow volatility." Risky lanes should remain visible through shadow/small-size instrumentation, while high-priority 1h and ETH 15m short candidates get a simplified previous-window/start-anchor entry and active profit-exit experiment.
