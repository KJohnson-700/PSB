# Strategy Entry Specification

This document defines how the bot should decide when to enter a trade and how to size it. Apply to arbitrage (mispricing) and consensus/fade strategies.

## Entry logic

**Target entry on the downside:** No hard floor. The bot should enter as long as the edge surpasses the minimum threshold, even if the price is below a prior “target” band (e.g. 55–65¢). Strong mispricings at 27¢ or 30¢ are valid entries when edge is sufficient.

**Maximum entry (upside cap):** A maximum entry price (e.g. 70¢) may be used so the bot never pays more than a set level; this is a risk/return preference, not a target band.

**Avoid chasing noise:** The bot must not chase random small dips. Entry requires that edge surpasses the configured minimum (e.g. `min_edge`, `ai_confidence_threshold`, or strategy-specific thresholds). This keeps entries tied to genuine mispricing or consensus edge, not short-lived noise.

**Strong, minor mispricings:** The bot should remain open to strong mispricings even when they appear “minor” in price terms—e.g. a small dip that reflects real edge (AI estimate vs market) is an acceptable entry. No target window or floor should block such entries.

## Position sizing and risk

- **Maximum position size** is governed by the Kelly Criterion and existing risk limits (e.g. `max_exposure_per_trade`, `max_position_size`, strategy-specific caps like `max_trade_size_pct` for fade).
- Entry logic (when to enter) is separate from sizing (how much). Sizing remains constrained by Kelly and risk config; entry remains flexible on the downside with no hard floor, subject to minimum edge and “no chasing small dips.”

## Summary

| Aspect | Rule |
|--------|------|
| Downside / target entry | Flexible; no hard floor. Enter when edge > min threshold even if price is below a prior target. |
| Max entry (optional) | May enforce a cap (e.g. never pay above 70¢) as a risk/return limit. |
| Chasing | Do not chase random small dips; require edge above threshold and, where applicable, AI confidence. |
| Strong minor mispricings | Allow entries when edge is sufficient; do not exclude low prices (e.g. 27¢, 30¢) by a target window. |
| Position size | Kelly Criterion and all configured risk limits continue to govern max size. |
