## RSI Post-Flip Gate Implementation

### Scope

Implemented only the P0 post-flip RSI/exhaustion re-gate in:

- `src/strategies/sol_macro.py`
- `src/strategies/eth_macro.py`

### Edited Lines

| Site | File | Lines | Window Arg | Placement |
|---|---|---:|---|---|
| SOL 5m block | `src/strategies/sol_macro.py` | 4806-4814 | `_updown_tf if is_updown else "15m"` | After `buy_no_5m_to_yes_flip`, before `_alt_buy_yes_bullish_floor_bump` and `_admission_prob`. |
| SOL 15m/1h block | `src/strategies/sol_macro.py` | 5118-5126 | `_updown_tf` | After fresh-cross/window-delta flips, post-flip disabled-side re-check, and low-ATR gate; before calibration, floor bump, and `_admission_prob`. |
| ETH up/down block | `src/strategies/eth_macro.py` | 2126-2134 | `_updown_tf` | After fresh-cross, `_window_delta_flip`, post-flip disabled-side re-check, low-ATR gate, calibration, and `eth_5m_no_to_yes_flip`; before `_adm_prob`/edge computation. |

### Control-Flow Confirmation

- `continue` is valid at all three insertion points; each site is inside the per-market scan loop and adjacent existing gates already use `_bump_skip(...)` plus `continue`.
- `_bump_skip("rsi_hard_blocked_postflip")` is valid at all three sites; the helper is already in scope and used throughout the same loops.
- The soft RSI delta from the post-flip pass is intentionally discarded (`_pf_hard, _ = ...`) so the soft penalty is not double-applied.
- The gate only acts on hard blocks from `_resolve_rsi_gate`; post-flip `BUY_YES` actions pass unless that helper changes semantics.

### Placement Notes

The SOL 15m/1h placement was slightly non-obvious because this branch has no posterior flip or buy-no-to-yes inversion after `_window_delta_flip`. The last action mutation before admission is `_window_delta_flip`, so the re-gate was placed after the existing post-flip disabled-side re-check and low-ATR gate, immediately before calibration and `_admission_prob` flow.

ETH has a later `eth_5m_no_to_yes_flip` after calibration. To ensure the RSI gate sees the genuinely final action, the re-gate was placed after that inversion and before `_adm_prob`/edge computation.

### Verification

```bash
.venv/bin/python -m py_compile src/strategies/sol_macro.py src/strategies/eth_macro.py
```

Result: passed.
