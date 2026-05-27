# BTC strategy audit — 2026-05-26

## Scope

Read-only audit of `bitcoin` performance using:

- `data/calibration/trades.jsonl` closed-trade calibration records
- `data/calibration/rejected_candidates_settled.jsonl` settled ghost candidates
- `src/strategies/bitcoin.py` side-selection / quant-flip code
- `config/settings.yaml` BTC strategy config

Latest closed BTC calibration timestamp: `2026-05-26T04:37:36Z`.

## Closed-trade summary

Last 24h BTC closed trades:

| slice | n | WR | PnL |
|---|---:|---:|---:|
| all BTC | 133 | 39.8% | +64.92 |
| 5m | 62 | 41.9% | +75.18 |
| 15m | 56 | 35.7% | -21.00 |
| 1h | 15 | 46.7% | +10.74 |

Last 6h BTC closed trades:

| slice | n | WR | PnL |
|---|---:|---:|---:|
| all BTC | 49 | 38.8% | +19.99 |
| 5m | 24 | 45.8% | +20.16 |
| 15m | 20 | 30.0% | -2.21 |
| 1h | 5 | 40.0% | +2.05 |

## Primary findings

### Finding 1 — BTC is not metadata-pinned like SOL-family alts

BTC lane regimes vary and use the BTC-only execution path. The SOL-family `primary/alt/btc` lane-collapse bug does not explain BTC underperformance.

### Finding 2 — 15m is the weak BTC window

The 24h 15m cohort is negative despite a positive 5m cohort:

| lane | n | WR | PnL |
|---|---:|---:|---:|
| `15m|down|bullish|standard` | 17 | 23.5% | -28.57 |
| `15m|down|bullish|drift` | 14 | 42.9% | -3.26 |
| `15m|down|bearish|standard` | 6 | 33.3% | -2.02 |

The largest positive lane is still `5m|down|bullish|standard`: n=39, WR=43.6%, PnL=+77.81.

### Finding 3 — Quant-flip is currently net negative

Last 24h by side source:

| side source | n | WR | PnL |
|---|---:|---:|---:|
| `btc_bull_rollover_countertrend` | 80 | 38.8% | +65.26 |
| `btc_htf_bias` | 42 | 40.5% | +13.34 |
| `btc_quant_disagree_flip` | 11 | 45.5% | -13.68 |

The quant-flip path is no longer the zero-trade fix it was intended to be. It now contributes negative PnL at small sample, mainly through unfavorable payout mix.

### Finding 4 — Ghosts say the rejected conflict cohort is strong

Last 24h settled BTC ghost candidates:

| reject reason | n | would-win rate |
|---|---:|---:|
| `lane_min_edge` | 529 | 53.9% |
| `lane_min_edge_bias_quant_disagree` | 414 | 62.8% |

This is scan-level ghost evidence, not fill-level PnL. Still, it is the strongest audit signal: the bot is rejecting many BTC candidates where the raw probability and HTF/market relationship disagree, and those rejected candidates are settling well.

### Finding 5 — Dead-zone config is currently diagnostic only

`config/settings.yaml` has `dead_zone_enabled: false` for BTC while `blocked_utc_hours_updown` still lists `[0, 1, 2, 7, 11]`. Closed trades appear during those hours because the block is off. This is not a bug, but it can confuse audit interpretation unless the report labels dead-zone skips as counterfactual only.

## Code-path notes

Relevant code:

- `src/strategies/bitcoin.py::_maybe_quant_flip`
- `src/strategies/bitcoin.py` 5m path calls `_maybe_quant_flip` after `compute_btc_5m_quant`
- `src/strategies/bitcoin.py` 15m/1h path calls `_maybe_quant_flip` after `est_prob_up` is built and clamped

No immediate execution bug was found in the edge formula:

- `BUY_YES`: `edge = estimated_prob - yes_price`
- `BUY_NO`: `edge = yes_price - estimated_prob`

One telemetry issue remains: `action_counts` is incremented before `_maybe_quant_flip`, so scan diagnostics can undercount post-flip action distribution.

## Audit conclusion

BTC is not failing from the alt bias-triple bug. BTC's current issue is a [[Regime Quant Disagreement]] problem: live trades are weak in 15m downside lanes, while ghost data says many rejected disagreement candidates would have settled correctly.

Do not infer that BTC should be broadly disabled from this audit. The next BTC-specific work should isolate why admitted 15m downside trades lose while rejected disagreement candidates win.

## Metadata / Summary

Tags: #PSB #BTC #StrategyAudit #GhostLog #QuantFlip #Polymarket

Related Concepts: [[BTC Quant Flip]], [[Lane Calibration]], [[Ghost Log]], [[Regime Quant Disagreement]], [[Polymarket UpDown]]

Summary: BTC is weak but for a different reason than SOL-family alts: its 15m downside lanes and quant-flip cohort are underperforming, while 5m remains the main positive contributor. Settled ghosts show the rejected `lane_min_edge_bias_quant_disagree` cohort has a high scan-level would-win rate, so the likely issue is selection/admission logic around disagreement, not a pinned classifier.
