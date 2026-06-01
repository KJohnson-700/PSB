# BUY_YES Last-Resort Playbook

## Purpose

This is an emergency operator note only. It is not active strategy code and must not be implemented without explicit operator approval.

## Last-Resort Conditions

Use this only if BUY_YES degradation creates unacceptable paper/live risk and the normal repair path cannot be completed in time.

- Confirm the damage from `data/calibration/trades.jsonl` or Ghost Lab.
- Prefer probability haircuts and min-edge adders before any lane pause.
- Do not stop or restart the local bot unless the operator explicitly approves it.

## Rejected Clamp Pattern

The rejected approach was a family/window allowlist that blocked broad BUY_YES traffic to lift reported WR. It was reverted because it improved the scoreboard by removing losers instead of fixing false positives.

If an emergency pause is explicitly approved later, document:

- exact strategy/window/family affected
- evidence window and sample size
- expected duration
- re-enable criteria
- confirmation that the operator approved a restart if needed

## Preferred Repair Path

Use lane-specific soft corrections:

- probability haircut when raw probability is overconfident
- min-edge add when a context is weak but not invalid
- oracle-basis min-edge add when payoff feed and exchange feed diverge
- report-only monitoring when WR is low but PnL remains positive
