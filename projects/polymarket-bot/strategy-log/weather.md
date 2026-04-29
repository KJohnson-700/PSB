# Weather

## Quick Stats

| Metric | Value | Source |
| --- | --- | --- |
| Closed trades | - | - |
| Win rate | - | - |
| Net PnL | - | - |
| Avg PnL / trade | - | - |

## Change Log

### 2026-04-28 — Borderline-only weather AI ensemble behind `use_weather_ai`
- **What changed:** Added a weather-specific 3-role AI ensemble runner for borderline weather markets only, wired it into `src/strategies/weather.py` behind `use_weather_ai: false`, and fed the ensemble probability back through the same corrected gap, EV, and binary-Kelly pipeline instead of creating a separate execution path.
- **Why:** The revised design is to use AI only where the quant edge is real but uncertain, not on clear quant signals. This keeps weather AI as a probability-refinement layer rather than a strategy override.
- **Hypothesis:** Restricting AI to `gap_min <= gap <= gap_min + borderline_high`, shrinking low-confidence ensemble output toward market price, and forcing `HOLD` on strong disagreement should improve marginal weather decisions without changing the core quant risk framework.
- **Expected outcome:** Clear weather edges should continue trading without AI calls, while borderline cases can be upgraded, vetoed, or flipped by an AI-adjusted probability that still has to clear the normal EV and sizing checks.
- **Actual outcome:** pending
- **Status:** pending

### 2026-04-28 — Binary Kelly, horizon-aware filters, and Paris station alignment
- **What changed:** Replaced weather’s edge-only bet sizing path with binary Kelly sizing that uses actual contract payout odds from price, changed the EV haircut from a flat `0.02` to a percentage-of-price fee buffer, scaled METAR mismatch tolerance by forecast horizon, added horizon-adaptive minimum EV thresholds, and kept Paris mapped to Le Bourget (`LFPB`) in live/backtest helpers.
- **Why:** The prior weather sizing treated all contracts like even-money bets, the flat fee buffer over-penalized cheap contracts, and the same temperature mismatch/EV thresholds were being applied to both near-dated and farther-dated forecasts despite different uncertainty regimes.
- **Hypothesis:** Price-aware Kelly sizing plus horizon-aware EV and METAR filters should preserve legitimate weather opportunities at low prices and longer horizons while still keeping near-resolution entries stricter and aligned with the correct Paris resolution airport.
- **Expected outcome:** Weather signals should size differently at different prices for the same probability edge, reject fewer valid T+2/T+3 forecasts on static METAR mismatch logic, and evaluate cheap contracts without the disproportionate haircut caused by a flat two-cent buffer.
- **Actual outcome:** pending
- **Status:** pending

### 2026-04-28 — Paris station corrected again to Le Bourget (`LFPB`)
- **What changed:** Corrected the Paris mapping in `src/strategies/weather.py` and `scripts/run_backtest_weather.py` from `LFPG` to `LFPB`, and aligned the local weather spec/test references.
- **Why:** The prior correction to Charles de Gaulle was wrong. Paris weather markets here should map to Le Bourget (`LFPB`), not `LFPG` or `LFPO`.
- **Hypothesis:** Restoring `LFPB` keeps Paris live forecasts and backtest assumptions aligned with the actual market resolution source used by this project.
- **Expected outcome:** Paris parsing should return `LFPB` consistently in live strategy and backtest support code.
- **Actual outcome:** pending
- **Status:** pending

### 2026-04-28 — Paris station corrected to Charles de Gaulle (`LFPG`)
- **What changed:** Updated Paris weather market mapping in `src/strategies/weather.py` and `scripts/run_backtest_weather.py` from `LFPB` to `LFPG`, with corresponding Charles de Gaulle coordinates. Corrected the local weather spec note to match.
- **Why:** Polymarket Paris temperature markets resolve from the Charles de Gaulle Airport Station and reference Wunderground `LFPG`; the prior Paris mapping pointed at the wrong airport.
- **Hypothesis:** Using the correct Paris station should align live weather forecasts, resolution assumptions, and backtest validation for Paris markets instead of pulling the wrong airport signal.
- **Expected outcome:** Paris weather signals and any future Paris backtests should reference `LFPG` consistently across live strategy and supporting docs.
- **Actual outcome:** pending
- **Status:** pending

### 2026-04-28 — Background weather refresh decoupled from scanner sync timeout
- **What changed:** Removed weather discovery from the per-cycle sync critical path in `src/market/scanner.py`. The scanner now keeps a single background weather refresh alive and returns the last completed weather snapshot immediately instead of dropping weather every time the 90-second sync budget expires.
- **Why:** Repeated partial sync timeouts were leaving `unfinished=['weather']` and returning zero weather markets to the strategy even when Gamma weather fetches were working. Restarting a new slow fetch every cycle risked permanent starvation.
- **Hypothesis:** A cached background refresh should let the bot continue scanning BTC/alts on schedule while still handing weather markets to the strategy as soon as one refresh completes.
- **Expected outcome:** Scanner logs should stop showing `weather_market_count: 0` solely because weather fetch missed the sync deadline. After the first completed background refresh, weather markets should reappear in strategy input even if refresh latency remains high.
- **Actual outcome:** pending
- **Status:** pending

### 2026-04-28 — City and airport alias normalization hardening
- **What changed:** Hardened weather market city resolution in `src/strategies/weather.py` with normalized aliases and slug-aware extraction for live Gamma market shapes such as `NYC`, `Boston Logan`, `Chicago, IL`, `ORD`, and similar airport/state variants.
- **Why:** Dedicated weather fetch was landing current precipitation and temperature markets, but strategy-side location parsing was too brittle against Gamma wording and slug variation, which risked collapsing weather analysis into zero usable matches.
- **Hypothesis:** Expanding canonical city and airport alias matching should let the strategy keep NYC/Boston/Chicago weather ladders in the analysis set instead of rejecting them on location parsing.
- **Expected outcome:** Weather scan diagnostics should show non-zero `city_matches` when dedicated weather markets are present, especially for NYC/Boston/Chicago ladders.
- **Actual outcome:** pending
- **Status:** pending

## Review Sessions

## Lessons Learned
