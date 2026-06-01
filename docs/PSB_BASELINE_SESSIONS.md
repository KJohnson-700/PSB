# PSB Baseline Session Reference

> Last updated: 2026-05-28 | Bot session: test_20260528_042826

---

## Session Summaries

### Session A — test_20260522_052210 ⭐ (reference good session)
| Field | Value |
|---|---|
| Trades | 199 |
| Realized PnL | **+$287.92** |
| Win Rate | 42.2% |
| Staked Notional | $1,630 |
| Bankroll (end) | $792.25 |
| Started | 2026-05-22T12:24 UTC |
| Last activity | 2026-05-22T17:13 UTC |

**Per-lane breakdown:**
| Lane | Trades | WR | PnL | Avg/trade |
|---|---|---|---|---|
| bitcoin | 67 | 37.3% | +$90.82 | +$1.36 |
| sol_macro | 30 | 53.3% | +$80.06 | +$2.67 |
| xrp_macro | 26 | 57.7% | +$78.48 | +$3.02 |
| eth_macro | 22 | 36.4% | +$10.11 | +$0.46 |
| doge_macro | 20 | 35.0% | +$6.60 | +$0.33 |
| bnb_macro | 20 | 45.0% | +$10.64 | +$0.53 |
| hype_macro | 14 | 28.6% | +$11.21 | +$0.80 |

**Key observations:** BTC and SOL were the top PnL contributors. XRP WR 58% solid. DOGE mediocre. HYPE low trades but positive.

---

### Session B — test_20260527_003152 (reference bad/short session)
| Field | Value |
|---|---|
| Trades | 27 |
| Realized PnL | **-$42.12** |
| Win Rate | 22.2% |
| Staked Notional | $170 |
| Bankroll (end) | $0 (wiped?) |
| Started | 2026-05-27T07:34 UTC |
| Last activity | 2026-05-27T11:02 UTC |

**Per-lane breakdown:**
| Lane | Trades | WR | PnL |
|---|---|---|---|
| bitcoin | 19 | 21.1% | -$37.62 |
| doge_macro | 5 | 20.0% | -$6.07 |
| bnb_macro | 2 | 0.0% | -$4.46 |
| sol_macro | 1 | 100% | +$6.02 |

**Key observations:** BTC destroyed. This session got killed — bankroll went to 0. Short session, early death. Ghost calibration not yet settled for this session (0 matches in calibration log).

---

### Session C — test_20260527_042014 ⭐ (reference good session)
| Field | Value |
|---|---|
| Trades | 203 |
| Realized PnL | **+$154.90** |
| Win Rate | 38.9% |
| Staked Notional | $1,680 |
| Bankroll (end) | $654.92 |
| Started | 2026-05-27T11:23 UTC |
| Last activity | 2026-05-27T15:40 UTC |

**Per-lane breakdown:**
| Lane | Trades | WR | PnL | Avg/trade |
|---|---|---|---|---|
| bitcoin | 84 | 36.9% | +$35.36 | +$0.42 |
| xrp_macro | 24 | 45.8% | +$52.15 | +$2.17 |
| doge_macro | 19 | 52.6% | +$58.63 | +$3.09 |
| sol_macro | 26 | 42.3% | +$16.96 | +$0.65 |
| eth_macro | 23 | 34.8% | +$2.13 | +$0.09 |
| bnb_macro | 17 | 35.3% | +$3.05 | +$0.18 |
| hype_macro | 10 | 20.0% | -$13.39 | -$1.34 |

**Key observations:** DOGE and XRP carried. BTC large volume (84 trades) but modest return. HYPE negative.

---

### Session D — test_20260528_042826 (CURRENT, ongoing)
| Field | Value |
|---|---|
| Trades | 126 (2 open) |
| Realized PnL | **+$38.81** (+$39.73 total) |
| Win Rate | 37.1% |
| Staked Notional | $989.26 |
| Bankroll | $538.77 |
| Started | 2026-05-28T11:30 UTC |
| Last activity | 2026-05-28T21:03 UTC |

**Per-lane breakdown:**
| Lane | Trades | WR | PnL | Avg/trade | vs Session A | vs Session C |
|---|---|---|---|---|---|---|
| doge_macro | 13 | **69.2%** | **+$47.97** | +$3.69 | +$3.36/trade | +$0.60/trade |
| xrp_macro | 7 | **71.4%** | **+$24.72** | +$3.53 | +$0.51/trade | +$1.36/trade |
| sol_macro | 12 | 33.3% | +$3.17 | +$0.26 | -$2.41/trade | -$0.39/trade |
| hype_macro | 8 | 25.0% | +$0.44 | +$0.06 | -$0.74/trade | +$1.40/trade |
| bitcoin | 49 | **30.6%** | **-$10.68** | -$0.22 | -$1.58/trade | -$0.64/trade |
| bnb_macro | 17 | 35.3% | -$8.48 | -$0.50 | -$1.03/trade | -$0.68/trade |
| eth_macro | 18 | **27.8%** | **-$18.34** | -$1.02 | -$1.48/trade | -$1.11/trade |

---

## Calibration State (as of 2026-05-28)

### Ghost Overtight Flags
From `performance_feedback.overtight_preview`:

| Lane | Ghost N | Ghost WR | Admitted N | Admitted WR | Recommended |
|---|---|---|---|---|---|
| `hype_macro\|15m\|down\|bearish` | 451 | **71.6%** | 18 | 77.8% | Loosen min_edge +0.0028 |
| `hype_macro\|15m\|up\|bullish` | 380 | **63.2%** | 15 | 60.0% | Loosen min_edge +0.0053 |

### Decision Gates
- **Enabled:** `false` — composite floor scoring NOT enforced
- **Ghost active blocks (current pulse):** DOGE (composite_score_below_floor ×2), BNB (×1)

### Performance Feedback
- **Enabled:** `false` — auto-loosening NOT active
- `overtight_contract.reasons`: only `lane_min_edge` watched
- Biggest rejection gates NOT watched: `iql_15m_reject` (11K), `lane_entry_window` (10K), `hist_gate_15m_short_reject` (7K)

### Scan Skip Digest (current session top rejects)
| Gate | Count |
|---|---|
| `lane_entry_window` | 26 |
| `neutral_bias` | 11 (DOGE only) |
| `ltf_confirmed_late_entry` | 9 (BTC) |
| `iql_15m_reject` | 9 (SOL) |
| `eth_15m_weak_confirm` | 9 (ETH) |

---

## Cross-Session Lane Trends

| Lane | Session A (May 22) | Session C (May 27 AM) | Session D (May 28) | Trend |
|---|---|---|---|---|
| **DOGE** | WR 35%, +$6.60 | WR 53%, +$58.63 | WR **69%**, +$47.97 | 🟢 Improving |
| **XRP** | WR 58%, +$78.48 | WR 46%, +$52.15 | WR **71%**, +$24.72 | 🟢 Improving |
| **BTC** | WR 37%, +$90.82 | WR 37%, +$35.36 | WR **31%**, -$10.68 | 🔴 Degrading |
| **ETH** | WR 36%, +$10.11 | WR 35%, +$2.13 | WR **28%**, -$18.34 | 🔴 Degrading |
| **BNB** | WR 45%, +$10.64 | WR 35%, +$3.05 | WR 35%, -$8.48 | 🔴 Flat→Down |
| **SOL** | WR 53%, +$80.06 | WR 42%, +$16.96 | WR 33%, +$3.17 | 🔴 Degrading |
| **HYPE** | WR 29%, +$11.21 | WR 20%, -$13.39 | WR 25%, +$0.44 | ⚠️ Consistently bad |

---

## Key Takeaways for AI Editors

1. **Ghost calibration is working** — lanes with poor ghost WR are being rejected. Overall ghost WR is 49.7% on 313K rows.
2. **DOGE and XRP are the profitable lanes** — they consistently win across sessions and carry the portfolio.
3. **BTC is the biggest trap** — high trade count, low WR, always risky. Ghost is blocking some but 48 trades still got through.
4. **Ghost two lanes for HYPE are overtight** — but those lanes barely get entries anyway (8 trades total).
5. **decision_gates disabled** means composite scoring isn't enforcing floors — dogs and BNB are getting through on score despite weak fundamentals.
6. **BTC skip digest** — `ltf_confirmed_late_entry` and `lane_entry_window` block a lot of BTC — those gates are saving the bankroll.
