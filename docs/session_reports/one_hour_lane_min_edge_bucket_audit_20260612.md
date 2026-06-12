## 1h Lane-Min-Edge Bucket Audit — 2026-06-12

### Scope

Request: build the missing bucketed report for 1h `lane_min_edge` ghosts by asset, side, and entry-price bucket before touching 1h thresholds.

Sources:

- `data/calibration/rejected_candidates_settled.jsonl`
- `config/settings.yaml`

Method:

- Filter: `window == "1h"` and `reason == "lane_min_edge"`.
- Strategies: `sol_macro`, `eth_macro`, `hype_macro`, `xrp_macro`, `doge_macro`, `bnb_macro`.
- Buckets use the settled ghost row's `entry_price_bucket`.
- WR intervals are Wilson 95% confidence intervals.
- `avg_ret` is average `realized_pct` from settled ghost outcomes. This is admission-side evidence only, not a proposal to hold positions to resolution.

Important limitation: ghosts validate admission-side rejects only. They do not model the current no-hold exit stack: stops, take-profit, slippage, time decay, queue position, or live fill quality.

Operator constraint: do **not** reintroduce hold-to-resolution. Any follow-up from this report must be validated under the current simple exit model.

### Current 1h Policy Map

All reviewed 1h lanes are already small-size forward-test lanes (`size_multiplier: 0.3`).

| Strategy | 1h BUY_YES min edge / band | 1h BUY_NO min edge / band |
|---|---:|---:|
| `sol_macro` | `0.07`, `0.42-0.58` | `0.07`, `0.42-0.58` |
| `eth_macro` | `0.09`, `0.42-0.58` | `0.09`, `0.42-0.58` |
| `hype_macro` | `0.06`, `0.42-0.58` | `0.06`, `0.42-0.58` |
| `xrp_macro` | `0.05`, `0.42-0.58` | `0.06`, `0.42-0.55` |
| `doge_macro` | `0.04`, `0.42-0.58` | `0.06`, `0.42-0.58` |
| `bnb_macro` | `0.08`, `0.42-0.58` | `0.08`, `0.42-0.58` |

### Summary By Lane

| Lane | n | WR | Wilson 95% | avg_ret | Read |
|---|---:|---:|---:|---:|---|
| `bnb_macro BUY_YES` | 623 | 89.2% | 86.6-91.4% | +0.161 | Strongest clean 1h min-edge miss. |
| `doge_macro BUY_YES` | 418 | 90.9% | 87.8-93.3% | +0.141 | Strong, but mostly expensive-YES bucket. |
| `sol_macro BUY_YES` | 603 | 87.9% | 85.0-90.3% | +0.117 | Strong, mostly expensive-YES bucket. |
| `xrp_macro BUY_NO` | 761 | 85.3% | 82.6-87.6% | +0.079 | Positive, but mostly low-YES / expensive-NO bucket. |
| `xrp_macro BUY_YES` | 612 | 81.2% | 77.9-84.1% | +0.026 | Positive but thinner per-trade value. |
| `sol_macro BUY_NO` | 1,390 | 71.8% | 69.4-74.1% | +0.034 | Positive aggregate; bucket split matters. |
| `bnb_macro BUY_NO` | 839 | 78.3% | 75.4-81.0% | +0.032 | Positive aggregate; cheap/expensive split matters. |
| `hype_macro BUY_NO` | 572 | 80.2% | 76.8-83.3% | +0.010 | High WR, weak value. |
| `eth_macro BUY_NO` | 45 | 82.2% | 68.7-90.7% | +0.282 | Too thin to act on alone. |
| `eth_macro BUY_YES` | 3,137 | 50.5% | 48.8-52.3% | +0.003 | No broad loosen. |
| `hype_macro BUY_YES` | 175 | 70.9% | 63.7-77.1% | -0.085 | High WR but negative value. |
| `doge_macro BUY_NO` | 613 | 73.2% | 69.6-76.6% | -0.095 | High WR but negative value. |

### Price-Bucket Detail

Only buckets with `n >= 50` are shown.

| Lane / bucket | n | WR | Wilson 95% | avg_ret | Read |
|---|---:|---:|---:|---:|---|
| `bnb_macro BUY_YES gt_0.57` | 538 | 92.0% | 89.4-94.0% | +0.122 | Strong, but verify exact prices because current band caps near `0.58`. |
| `bnb_macro BUY_YES 0.49_0.51` | 58 | 75.9% | 63.5-85.0% | +0.516 | Smaller sample, very high value. |
| `doge_macro BUY_YES gt_0.57` | 372 | 94.6% | 91.8-96.5% | +0.136 | Strong, but expensive-YES bucket. |
| `sol_macro BUY_YES gt_0.57` | 520 | 92.5% | 89.9-94.5% | +0.113 | Strong, same exact-price caveat. |
| `xrp_macro BUY_YES gt_0.57` | 539 | 84.2% | 80.9-87.1% | +0.018 | Positive WR, modest value. |
| `bnb_macro BUY_NO lt_0.43` | 719 | 82.3% | 79.4-85.0% | +0.026 | High WR, small value because NO is expensive. |
| `bnb_macro BUY_NO 0.49_0.51` | 91 | 64.8% | 54.6-73.9% | +0.297 | More attractive than low-YES bucket; smaller sample. |
| `sol_macro BUY_NO lt_0.43` | 832 | 83.8% | 81.1-86.1% | +0.009 | High WR, almost flat value. |
| `sol_macro BUY_NO 0.49_0.51` | 512 | 52.5% | 48.2-56.8% | +0.048 | Lower WR, better value than low-YES bucket. |
| `xrp_macro BUY_NO lt_0.43` | 682 | 87.1% | 84.4-89.4% | +0.050 | Positive. |
| `hype_macro BUY_NO lt_0.43` | 564 | 80.9% | 77.4-83.9% | +0.015 | Too small value for broad action. |
| `doge_macro BUY_NO lt_0.43` | 575 | 75.7% | 72.0-79.0% | -0.082 | Do not loosen from WR alone. |
| `hype_macro BUY_YES gt_0.57` | 168 | 72.0% | 64.8-78.3% | -0.080 | Do not loosen from WR alone. |
| `eth_macro BUY_YES 0.46_0.49` | 229 | 45.0% | 38.7-51.5% | -0.055 | Bad. |
| `eth_macro BUY_YES 0.49_0.51` | 2,635 | 50.3% | 48.4-52.2% | +0.004 | Flat. |
| `eth_macro BUY_YES 0.51_0.54` | 195 | 52.3% | 45.3-59.2% | +0.005 | Flat. |

### Recent Check

Post `2026-06-11T00:00Z`, buckets with `n >= 25`:

| Lane / bucket | n | WR | Wilson 95% | avg_ret | Read |
|---|---:|---:|---:|---:|---|
| `bnb_macro BUY_YES gt_0.57` | 94 | 100.0% | 96.1-100.0% | +0.049 | Still positive, but value lower than all-history. |
| `xrp_macro BUY_YES gt_0.57` | 109 | 96.3% | 90.9-98.6% | +0.143 | Strong recent. |
| `sol_macro BUY_YES gt_0.57` | 102 | 92.2% | 85.3-96.0% | +0.026 | Positive but small value. |
| `doge_macro BUY_YES gt_0.57` | 55 | 96.4% | 87.7-99.0% | +0.005 | WR high, value nearly flat recently. |
| `xrp_macro BUY_NO lt_0.43` | 61 | 85.2% | 74.3-92.0% | +0.142 | Strong recent. |
| `sol_macro BUY_NO lt_0.43` | 48 | 79.2% | 65.7-88.3% | +0.160 | Positive recent; sample still small. |
| `bnb_macro BUY_NO lt_0.43` | 44 | 75.0% | 60.6-85.4% | +0.054 | Positive recent; sample still small. |
| `eth_macro BUY_NO lt_0.43` | 25 | 92.0% | 75.0-97.8% | +0.312 | Interesting, but too thin. |
| `eth_macro BUY_YES 0.49_0.51` | 351 | 36.2% | 31.3-41.3% | -0.277 | Actively bad recently. |
| `eth_macro BUY_YES 0.51_0.54` | 36 | 19.4% | 9.8-35.0% | -0.627 | Actively bad recently. |
| `hype_macro BUY_YES gt_0.57` | 50 | 74.0% | 60.4-84.1% | -0.101 | Still negative value. |
| `doge_macro BUY_NO lt_0.43` | 30 | 40.0% | 24.6-57.7% | -0.437 | Bad recent. |

### Findings

#### 1. `lane_min_edge` is not globally too strict

The evidence is lane- and bucket-specific. Some rejects are clearly missed value, but several high-WR cohorts are negative after price is considered.

Do not loosen `lane_min_edge` globally across 1h alts.

#### 2. Best follow-up candidates

Most defensible cohorts for a narrow follow-up:

| Candidate | Why |
|---|---|
| `bnb_macro 1h BUY_YES`, especially `gt_0.57` and near-even | Best all-history value; already forward-tested at `0.3x`. |
| `xrp_macro 1h BUY_YES gt_0.57` | Modest all-history value but strong recent sample. |
| `xrp_macro 1h BUY_NO lt_0.43` | Positive all-history and recent value, but NO is expensive when YES is low. |
| `sol_macro 1h BUY_YES gt_0.57` | Positive all-history; recent value is smaller, so forward-test only. |

#### 3. Do-not-loosen cohorts

| Cohort | Why |
|---|---|
| `eth_macro 1h BUY_YES` | Near-even buckets are flat all-history and sharply negative since 2026-06-11. |
| `doge_macro 1h BUY_NO lt_0.43` | Negative all-history and bad recent. |
| `hype_macro 1h BUY_YES gt_0.57` | WR looks good but value is negative. |
| `hype_macro 1h BUY_NO lt_0.43` | High WR but tiny value; not a priority. |

### Recommended Next Step

No config change from this report alone.

Before touching 1h thresholds, run a second pass that joins these cohorts to:

1. exact `yes_price` / trade-leg price, not only `entry_price_bucket`;
2. current live entry-band eligibility;
3. realized behavior under the current no-hold TP/stop exit stack in `trades_settled.jsonl`;
4. whether `alt_1h_simple_long` or AI-marginal bypass already captures the same cohort.

The current bot is already moving in the right direction with 1h lanes throttled at `0.3x`; the safer path is one cohort at a time, not a broad threshold cut.

### Metadata/Summary

Tags: #PSB #GhostLab #LaneMinEdge #AltLanes #QuantResearch

Related Concepts: [[Ghost Log Validation]], [[Lane Entry Policy]], [[1h Alt Lanes]], [[Entry Price Buckets]], [[Wilson Confidence Interval]]

Summary: The 1h `lane_min_edge` ghost audit shows real missed admission value, especially in BNB/XRP/SOL long-side cohorts, but it is not a global threshold problem and does not justify hold-to-resolution. Several high-WR buckets are negative once entry price is considered, so the next step should be exact-price/live-band/no-hold-exit joining before any config changes.
