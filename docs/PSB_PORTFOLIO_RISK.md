# Portfolio risk — correlated crypto up/down

Multiple strategies (SOL, ETH, HYPE, XRP) can fire on the **same BTC impulse** within one **cycle** or overlapping short windows. Independent **Kelly-sized** entries stack correlated exposure.

## Existing Controls

- **Global position cap:** all active PSB execution lanes are crypto up/down, so `max_concurrent_positions` applies to the whole bot.
- **Term budget:** `evaluate_entry` caps aggregate exposure by market term using `term_risk.caps` from config. Short-window crypto markets normally consume the **SHORT_TERM** cap.
- **Stop-cascade halt:** `correlation_stop_halt` blocks future same-side entries after clustered stop-loss exits.
- **Pre-entry basket guard:** `correlation_entry_guard` blocks correlated same-side crypto baskets before stop losses arrive:
  - `max_same_side_open: 4`
  - `max_same_side_short_window_open: 3`
  - `max_same_side_same_end_open: 2`

## Incident Note — 2026-06-12 Local Session

Session `test_20260612_123131` reproduced the prior gap: several same-side crypto up/down positions stacked into overlapping 5m/15m windows, then the stop-cascade breaker reacted only after losses had already printed. The fix is a **pre-entry exposure guard**, not hold-to-resolution and not a per-lane threshold guess.

## Remaining Gaps

- **Notional-aware haircut:** current guard counts positions, not dollar notional. A later version can reduce size for second/third correlated legs instead of hard-blocking.
- **Correlation score weighting:** current guard uses same side + window/end-time overlap, not live pairwise correlation magnitude.

## Operator action

1. Review **`/api/ops/summary`** → **`scan_skip_digest`** for starvation vs stacking.
2. Set **`bankroll`** and **`max_position_size`** so simultaneous full-tier trades cannot breach session risk tolerance.
