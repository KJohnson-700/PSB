## Strategy test review — post-merge BUY_YES / BUY_NO audit — 2026-05-18
### Summary

- Scope: post-resolver era starting **2026-05-17 04:11 America/Los_Angeles** (`2026-05-17T11:11:00Z`), anchored to resolver commits `0c91999`, `ca4ec48`, `5e4f018`, plus later same-day additive override work `57f4f23`.
- Conclusion: the **resolver merge did land**, and `BUY_YES` is **not absent** after the update, but `BUY_YES` is **not proven fixed** across the macro strategies. `BUY_NO` also still has lane-specific gate/calibration issues.
- `bitcoin` remains the clearest consistently working path. `hype_macro` shows meaningful `BUY_YES` flow, but it should not be treated as clean proof that the shared macro BUY_YES path is repaired because HYPE has more standalone logic and its own gate mix.
- The gating system is still directionally useful overall, but several **post-merge** gate families remain clearly overtight or lane-wrong, especially on BTC histogram short rejects and some 1h alt gates.

### Findings

| ID | Severity | Area | Evidence | Notes |
|----|----------|------|----------|-------|
| F1 | high | BUY_YES repair status | Post-merge closed-trade data in [data/calibration/trades.jsonl](/Users/mainfolder/Documents/psb-main%201/data/calibration/trades.jsonl) shows `eth_macro` `BUY_YES` only `8` trades on `15m` for `-0.73` PnL, `xrp_macro` `BUY_YES` `7` total trades for `+4.74`, `sol_macro` only `1` `BUY_YES` trade, while `bitcoin` remains entirely `BUY_NO` in this dataset | The update restored some LONG activity, but not enough to claim BUY_YES is repaired outside BTC-adjacent/HYPE contexts. |
| F2 | high | BTC short hist gates still block value | Post-merge settled ghosts in [data/calibration/rejected_candidates_settled.jsonl](/Users/mainfolder/Documents/psb-main%201/data/calibration/rejected_candidates_settled.jsonl) show `bitcoin|15m|BUY_NO|hist_gate_15m_short_reject` `n=3974`, `WR=57.4%`, `netGate=-634.41`; `bitcoin|1h|BUY_NO|hist_gate_1h_short_reject` `n=1715`, `WR=63.6%`, `netGate=-495.21` | This is still the strongest evidence of a structurally bad gate family. |
| F3 | high | ETH short-side 1h follow gate looks wrong | Post-merge settled ghosts show `eth_macro|1h|BUY_NO|btc_1h_not_following` `n=679`, `WR=69.7%`, `netGate=-277.91` | The system is skipping a large set of winning ETH short ghosts at 1h. |
| F4 | high | Some liquidity gates are lane-wrong, not globally protective | Post-merge settled ghosts show `sol_macro|1h|BUY_YES|liquidity` `n=625`, `WR=67.7%`, `netGate=-217.99`; `xrp_macro|1h|BUY_NO|liquidity` `n=1037`, `WR=58.7%`, `netGate=-182.12`; `sol_macro|BUY_YES|liquidity` aggregate remains negative | Liquidity should be treated as lane-aware. The same family is protective in some places and harmful in others. |
| F5 | medium | BUY_YES is present post-merge, but quality is strategy-specific | Post-merge closed trades: `hype_macro` `15m BUY_YES` `n=23`, `WR=43.5%`, `PnL=+17.55`; `hype_macro` `5m BUY_YES` `n=12`, `WR=25.0%`, `PnL=-19.32`; `xrp_macro` `15m BUY_YES` `n=6`, `WR=50.0%`, `PnL=+10.12`; `eth_macro` `15m BUY_YES` `n=8`, `WR=37.5%`, `PnL=-0.73` | The right question is lane-by-lane BUY_YES quality, not just “did BUY_YES occur.” |
| F6 | medium | ETH weak-confirm gates still look protective on both sides | Post-merge settled ghosts show `eth_macro|15m|BUY_NO|eth_15m_weak_confirm` `n=4961`, `netGate=+419.01`; `eth_macro|1h|BUY_NO|eth_1h_weak_confirm` `n=3019`, `netGate=+379.14`; `eth_macro|1h|BUY_YES|eth_1h_weak_confirm` `n=855`, `netGate=+332.71` | These are not obvious candidates for loosening. |
| F7 | medium | Sol/XRP catalyst and lane-min-edge loosening should be conservative | Post-merge settled ghosts show `sol_macro|5m|BUY_NO|no_btc_catalyst_5m` `n=1058`, `WR=52.8%`, `netGate=-65.87`; `xrp_macro|5m|BUY_NO|no_btc_catalyst_5m` `n=1005`, `WR=51.4%`, `netGate=-44.51`; `xrp_macro|1h|BUY_NO|lane_min_edge` `n=96`, `WR=90.6%`, `netGate=-28.23` | There is evidence of overtightness, but not enough to justify aggressive automatic negative-edge loosening. |

### Post-merge closed-trade side summary

Data source: [data/calibration/trades.jsonl](/Users/mainfolder/Documents/psb-main%201/data/calibration/trades.jsonl), rows with `ts >= 2026-05-17T11:11:00Z`.

| Strategy | Window | Side | n | WR | PnL |
|----------|--------|------|---:|---:|---:|
| `bitcoin` | `5m` | `BUY_NO` | 42 | 42.9% | `+7.57` |
| `bitcoin` | `15m` | `BUY_NO` | 27 | 44.4% | `+17.50` |
| `bitcoin` | `1h` | `BUY_NO` | 11 | 45.5% | `+23.10` |
| `eth_macro` | `5m` | `BUY_NO` | 9 | 55.6% | `+10.76` |
| `eth_macro` | `15m` | `BUY_NO` | 24 | 45.8% | `+21.06` |
| `eth_macro` | `15m` | `BUY_YES` | 8 | 37.5% | `-0.73` |
| `hype_macro` | `5m` | `BUY_YES` | 12 | 25.0% | `-19.32` |
| `hype_macro` | `15m` | `BUY_NO` | 5 | 40.0% | `+1.15` |
| `hype_macro` | `15m` | `BUY_YES` | 23 | 43.5% | `+17.55` |
| `sol_macro` | `5m` | `BUY_NO` | 2 | 0.0% | `-3.43` |
| `sol_macro` | `15m` | `BUY_NO` | 5 | 80.0% | `+21.73` |
| `sol_macro` | `1h` | `BUY_NO` | 2 | 100.0% | `+10.27` |
| `sol_macro` | `1h` | `BUY_YES` | 1 | 100.0% | `+2.66` |
| `xrp_macro` | `5m` | `BUY_NO` | 3 | 33.3% | `-8.14` |
| `xrp_macro` | `5m` | `BUY_YES` | 1 | 0.0% | `-5.38` |
| `xrp_macro` | `15m` | `BUY_NO` | 17 | 29.4% | `-7.64` |
| `xrp_macro` | `15m` | `BUY_YES` | 6 | 50.0% | `+10.12` |
| `xrp_macro` | `1h` | `BUY_NO` | 4 | 25.0% | `-5.86` |

### Strategy observations

- `BUY_YES` is **not missing** after the resolver update. It is present in `eth_macro`, `hype_macro`, `sol_macro`, and `xrp_macro`.
- `BUY_YES` is also **not uniformly healthy**:
  - `eth_macro` LONG sample is small and slightly negative.
  - `xrp_macro` `15m BUY_YES` is promising but still tiny.
  - `sol_macro` has almost no LONG sample yet.
  - `hype_macro` `15m BUY_YES` is positive, but `5m BUY_YES` is poor.
- `BUY_NO` improved post-merge in several places:
  - `bitcoin 5m BUY_NO` moved from pre-merge `-39.68` on `89` trades to post-merge `+7.57` on `42`.
  - `eth_macro 15m BUY_NO` moved from pre-merge `-17.14` on `11` trades to post-merge `+21.06` on `24`.
- `xrp_macro` remains mixed and likely under-calibrated on both sides:
  - `15m BUY_YES` is positive on small sample.
  - `15m BUY_NO` remains negative.
  - `5m` is poor on both sides in this post-merge slice.

### Gate observations

- **Still likely bad / overtight post-merge**
  - `bitcoin` short histogram reject family (`15m`, `1h`)
  - `eth_macro` `btc_1h_not_following` on `BUY_NO`
  - `sol_macro` `1h BUY_YES liquidity`
  - `xrp_macro` `1h BUY_NO liquidity`
- **Still likely useful / protective post-merge**
  - `eth_macro` `eth_15m_weak_confirm`
  - `eth_macro` `eth_1h_weak_confirm`
  - `hype_macro BUY_NO liquidity`
  - several `lane_entry_window` families on HYPE/XRP/SOL
- **Conclusion:** gate work should be **lane-aware and side-aware**, not global-family-only.

### Likely bugs / miscalculations

1. **Resolver fix landed, but downstream gate calibration still conflicts with intended behavior.** The side resolver no longer appears to be the primary blocker; the larger remaining issue is that downstream gates still suppress or distort good lanes unevenly by side.
2. **Liquidity is acting like a coarse global veto when the data says it behaves differently by strategy, side, and timeframe.** This is a design mismatch, not just a threshold issue.
3. **BTC short histogram rejects still look directionally inverted relative to realized outcomes.** The post-merge data still says these rejects are blocking more value than they protect.

### Suggested improvements (prioritized)

1. **Do not assume BUY_YES is fixed.** Continue tracing `BUY_YES` and `BUY_NO` separately by `strategy × window × side × lane family`.
2. **Keep adaptive loosening off until this audit is operationalized.** The right first move is better measurement and targeted manual calibration, not broad runtime self-widening.
3. **Make gate review lane-aware.** Start with:
   - `bitcoin` short histogram rejects
   - `eth_macro` `btc_1h_not_following`
   - `sol_macro` and `xrp_macro` liquidity on the specific 1h lanes called out above
4. **Do not loosen ETH weak-confirm gates yet.** Current post-merge ghost economics still say those gates save more than they cost.
5. **Separate HYPE from shared-macro BUY_YES conclusions.** Use HYPE as its own calibration target; do not let it stand in for SOL/ETH/XRP macro LONG health.
6. **If adaptive min-edge loosening is enabled later, start conservatively.** Suggested initial config:

```yaml
performance_feedback:
  enabled: false
  overtight_reasons: ["lane_min_edge"]
  overtight_min_lane_sample: 25
  overtight_min_pass_sample: 12
  overtight_ghost_wr_threshold: 0.58
  overtight_max_relax_delta: 0.03
  overtight_min_edge_mult_floor: 0.70
  overtight_min_edge_mult_ceil: 1.0
```

7. **After any lane changes, re-run this exact post-merge slice as a regression check.** Success criterion is not just more LONGs or more SHORTs; it is better lane-level economics with fewer clearly negative-net gates.

### Open questions

- Whether the post-merge BUY_YES samples for `eth_macro`, `sol_macro`, and `xrp_macro` are still too small for a clean claim, or whether they are already showing a real structural weakness.
- Whether some of the negative-net BTC / ETH / SOL / XRP gate families should be converted from hard rejects into softer probability or sizing adjustments.

### Metadata / Summary

- **Tags:** `#PSB` `#BUY_YES` `#BUY_NO` `#GateAudit` `#GhostTrades` `#LaneCalibration`
- **Related Concepts:** `[[Four-Path Resolver]]`, `[[Lane-Aware Gates]]`, `[[Adaptive Loosening]]`, `[[Ghost Gate Report]]`
- **Summary:** The May 17 resolver merge is present in code and post-merge data shows both BUY_YES and BUY_NO activity, so the current problem is no longer “BUY_YES completely absent.” The remaining issue is calibration: BUY_YES is still not proven healthy outside BTC-adjacent/HYPE contexts, and several post-merge gate families remain demonstrably overtight or lane-wrong.
