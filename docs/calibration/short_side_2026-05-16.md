# SHORT-side calibration — 2026-05-16

**Goal**: make the losing lanes win and capture more of what the winners already do. No min_edge raises, no admission-tightening, no disables. Logic changes that move the probability estimate or open new entry paths.

## Data

- 302 taken trades, all SHORT (regime-locked bearish), 17 lane-families with n≥6.
- 3,081 settled rejected ghosts (BTC only — macro hooks shipped today, no data yet).

## Per-lane results

| Lane | Family | n | WR | avg RP |
|---|---|---|---|---|
| btc 15m down bearish | drift | 18 | 66.7% | **+0.367** |
| xrp_macro 15m down | spike | 11 | 54.5% | +0.224 |
| btc 15m down bearish | predict_window | 19 | 36.8% | +0.075 |
| xrp_macro 5m down | standard | 20 | 45.0% | +0.099 |
| btc 15m down bearish | standard | 31 | 38.7% | +0.011 |
| eth_macro 15m down | drift | 7 | 28.6% | -0.058 |
| btc 5m down bearish | drift | 33 | 33.3% | -0.088 |
| btc 5m down bearish | standard | 32 | 31.2% | -0.089 |
| sol_macro 5m down | standard | 27 | 29.6% | -0.090 |
| eth_macro 5m down | standard | 25 | 28.0% | -0.150 |
| sol_macro 15m down | standard | 6 | 0.0% | -0.278 |
| xrp_macro 15m down | standard | 7 | 0.0% | -0.402 |
| hype_macro 15m down | standard | 8 | 0.0% | **-0.442** |

Ghost comparison (BTC only — gate behavior):

| | n | WR | avg RP |
|---|---|---|---|
| BTC 5m down — **rejected** ghosts | 588 | 49.0% | **-0.019** |
| BTC 5m down — taken trades | 72 | 33.3% | **-0.087** |
| BTC 15m down — rejected ghosts | 2489 | 49.4% | -0.005 |
| BTC 15m down — taken trades | 68 | 45.6% | +0.099 (avg across families) |

## Calibration moves

### Move 1 — Convert the BTC 5m hist_gate rejection into a contrarian SHORT signal

**Observation**: The 5m SHORT gate at [bitcoin.py:1310](src/strategies/bitcoin.py:1310) rejects when both 4H and 1H histograms are rising. Those 588 rejected ghosts averaged -0.019 RP; the trades we actually took averaged -0.087 RP. The pattern we're throwing away outperforms the pattern we keep.

**Why this happens (hypothesis)**: 4H+1H both rising hard against trade direction in a bearish regime is usually an exhausted counter-trend bounce — the kind of setup that reverses inside the next 5m window. Currently we reject because "no momentum building for SHORT." That framing is backwards: the absence of building short momentum *combined with* an overstretched bull bounce is exactly the mean-reversion setup that pays.

**Calibration**:
- Don't reject the pattern. Route it into a new entry family — call it `counter_trend` — that fires SHORT specifically when 4H AND 1H histograms are both rising against trade direction in bearish regime.
- The est_prob_up adjustment for this family should be *opposite-signed* to the standard 5m m5_adj path: when m5 momentum is SPIKE_UP / DRIFT_UP (currently a penalty for SHORT), boost est_prob_down instead — fade the spike.
- Sizing for the new family inherits the existing 5m sizing; no Kelly change in v1. Just route the trade.
- Add the family to `resolve_entry_family` in [src/analysis/lane_identity.py:54](src/analysis/lane_identity.py:54) (one new branch for "counter_trend_" prefix).

**Expected impact**: even if the new family only matches the rejected ghosts' -0.019 RP, that's a $0.07 per-trade improvement over the taken-trade baseline. If the mean-reversion thesis is right, it should do meaningfully better than the rejected average because we'll exit on a 5m horizon rather than letting the market resolve.

**Files**: [src/strategies/bitcoin.py:1310–1332](src/strategies/bitcoin.py:1310) (replace rejection with branch), [src/analysis/lane_identity.py:54](src/analysis/lane_identity.py:54) (new family token).

### Move 2 — Stop ignoring the 1H histogram in 15m macro SHORT entries

**Observation**: Every losing macro lane shares the regime tag `bearish__bearish__bull` — alt macro bear + primary macro bear + **BTC 1H bull**. The bull suffix means we're SHORTing an alt while BTC's short-term tape is going up. Currently [sol_macro.py:2131–2150](src/strategies/sol_macro.py:2131) explicitly logs "1H histogram against short_15m" but is **diagnostic-only** — the disagreement does not move est_prob_up. That's the bleed.

**Calibration**:
- Activate the 1H histogram alignment as a probability dampener (not a hard gate). When `_h1_bear_ok` is False for a SHORT entry in 15m, subtract from est_prob_down (i.e. dampen the short-side conviction). Suggested magnitude: -0.04 (matches the size of `_apply_primary_htf_bias` boost, so it cancels half of the macro tilt when 1H disagrees).
- Apply symmetrically for LONG when `_h1_bull_ok` is False.
- Same change in [eth_macro.py](src/strategies/eth_macro.py) since it has its own scan loop.
- **Effect on winners (drift, spike, predict_window)**: those families already have positive m5/15m momentum (which is what flagged them as drift/spike). When momentum agrees with trade direction, 1H histogram is usually also aligned, so the dampener wouldn't fire. The bleed is specifically in *standard* (the residual family — no momentum reason fired), where 1H disagreement is most common.

**Files**: [src/strategies/sol_macro.py:2131–2150](src/strategies/sol_macro.py:2131) and the parallel block for 5m at ~1890. Same edits to [eth_macro.py:802–825](src/strategies/eth_macro.py:802) and the 5m block.

### Move 3 — Add a BTC-1H-regime-aware macro tilt for the 15m down path

**Observation**: `_apply_primary_htf_bias` adds a flat -0.07 est_prob_up shift for SHORT in bearish regime ([sol_macro.py:2123–2126](src/strategies/sol_macro.py:2123)). That -0.07 doesn't know whether BTC's 1H mini-regime agrees. When BTC 1H is BULL (i.e. the `bearish__bearish__bull` tag), the -0.07 over-states our actual conviction.

**Calibration**:
- Scale the -0.07 by a regime-agreement multiplier:
  - BTC 1H BEAR: 1.0× (full conviction, regime agrees)
  - BTC 1H RANGE: 0.7× (reduced conviction)
  - BTC 1H BULL: 0.4× (low conviction, regime disagrees with SHORT)
- This is opposite-direction from the existing `btc_1h_regime_gates.min_edge_mult` config (which tightens min_edge in BEAR — wrong direction for our problem). What I'm proposing is a probability scaler that lives inside the strategy, not a min_edge scaler.
- Add `_btc_1h_regime_prob_mult(regime, side)` helper on `SolMacroStrategy` and apply at the macro bias step.

**Files**: [src/strategies/sol_macro.py:2123](src/strategies/sol_macro.py:2123) (15m path), [~1880](src/strategies/sol_macro.py:1880) (5m path), parallel in eth_macro.

### Move 4 — Mirror XRP's `require_btc_catalyst_5m: true` to sol and eth

**Observation**: XRP 5m down standard is the *only* macro 5m down lane that's net positive (+0.099 RP). XRP is also the only macro with `require_btc_catalyst_5m: true`. The other three (sol, eth, hype) bleed similarly on 5m down standard. The most likely causal mechanism: XRP's catalyst requirement forces an actual BTC move before SHORT-side 5m entries fire, filtering out flat-tape no-edge entries.

This is a logic gate (catalyst presence), not a threshold raise — it doesn't "tighten" anything that already had a theory of edge, it requires the precondition of the theory (BTC moving) to be met.

**Calibration**:
- Set `require_btc_catalyst_5m: true` in both `sol_macro` and `eth_macro` blocks in [config/settings.yaml](config/settings.yaml) (sol ~line 510, eth ~line 700).
- HYPE may want this too but verify against HYPE's specific lag/correlation behavior first.

### Move 5 — Capture more of the BTC 15m drift winner

**Observation**: btc 15m down bearish **drift** is the strongest lane in the dataset: 66.7% WR, +0.367 RP, n=18. Per `feedback_no_tightening_dont_tweak_winners.md` no admission changes. But there's a sizing question worth asking.

**Calibration** (only if you want it — pinging because the data is so strong):
- Current per-trade sizing uses Kelly with the strategy-wide `kelly_fraction`. The drift lane has 66.7% WR vs strategy avg ~40%, but it's sized the same as the other 15m families.
- A per-lane Kelly multiplier (lane_id → size scalar) would let this lane size up to its actual edge. Add a `lane_size_multiplier` field to lane state, default 1.0, with manual overrides for proven winners.
- Concrete: lane_size_multiplier of 1.5–2.0 on `bitcoin|15m|down|bearish|drift` based on its posterior. n=18 is borderline for confidence — re-check after n=30+ before committing.
- **Not for this PR. Tee up after Moves 1–4 ship and we have macro ghost data.**

## Verification path

After implementing Moves 1–4, with macro hooks shipped today already capturing data:

1. **48 hours of run-time** to accumulate macro rejected-ghost outcomes (sol/eth/xrp/hype gates: `no_btc_catalyst_5m`, `weak_5m_signal`, `iql_15m_reject`, etc.). Same posterior backfill flow as BTC.
2. **Re-run the per-lane WR/avg-RP analysis** filtered to trades opened after the change. The losing standard lanes should converge toward break-even rather than -0.10 to -0.44 RP.
3. **For Move 1 specifically**: track the new `counter_trend` lane separately. If after n=20 it's underwater, the inversion hypothesis was wrong and back it out — no other moves depend on it.

## Files this calibration touches (none yet)

- `src/strategies/bitcoin.py` — Move 1
- `src/strategies/sol_macro.py` — Moves 2, 3
- `src/strategies/eth_macro.py` — Moves 2, 3
- `src/analysis/lane_identity.py` — Move 1 (one-line family token addition)
- `config/settings.yaml` — Move 4 (two lines)
