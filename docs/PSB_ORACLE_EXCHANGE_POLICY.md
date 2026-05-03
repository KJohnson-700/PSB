# Oracle vs exchange alignment (policy)

Short-window Polymarket crypto markets resolve against a stated oracle path; live signals often use **exchange** OHLCV / momentum.

## Config knobs (audit annually)

| Setting | Typical location | Meaning |
|--------|-------------------|--------|
| `skip_on_degraded_correlation` | `strategies.*_macro` | Skip when BTC↔alt correlation service marks **degraded** (sol_macro implements gate path). |
| `enforce_alt_1h_alignment` | `strategies.*_macro` | Require alt 1H trend alignment with direction — currently often **`false`** for flexibility. |
| `oracle_max_basis_bps` | `eth_macro` | ETH Chainlink vs exchange basis veto. |

## Operator stance

- Treat **severe oracle vs exchange disagreement** as an **entry disqualifier**, not a soft “size a bit smaller” lever — unless you explicitly model basis as signal.
- **`skip_on_degraded_correlation: true`** (when present) aligns with treating degraded correlation as **untrustworthy lag**, not a sizing tweak.

## Code references

- `src/strategies/sol_macro.py`: degraded correlation and alignment gates.
- `src/strategies/eth_macro.py`: oracle basis / ETH-specific paths.
