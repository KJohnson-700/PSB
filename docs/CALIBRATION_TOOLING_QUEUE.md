# Calibration Tooling Queue

## Implemented Now

| Item | Status | Notes |
|---|---|---|
| Reliability diagrams + Brier decomposition | implemented | `scripts/probability_diagnostics.py` writes report JSON/Markdown plus pooled SVG reliability chart. |
| Empirical endpoint probability baseline | implemented | Leave-one-out conditioned baseline against resolved trades plus settled rejected candidates. |
| Market / constant baselines in every report | implemented | Included in overall and lane rows. |
| Time-aware evaluation scaffolding | implemented | `src/analysis/time_aware_split.py` provides chronological purged folds for downstream supervised evaluation. |

## Queue Next

| Item | Status | Trigger |
|---|---|---|
| Pooled isotonic recalibration | queued after 1-4 | Only if the diagnostics show usable resolution with calibration error that a pooled map can improve. |
| Take-rate / selection-bias extensions | queued after 1-4 | Current report includes take-rate; any IPW or exploration design should wait for the new diagnostics to settle. |

## Review Soon, Do Not Add Yet

| Item | Status | Why deferred |
|---|---|---|
| Meta-labeling | review soon, not added | Too easy to learn gate bias before the new report stack is the standard evaluation path. |
| `river` online updates / drift detectors | review soon, not added | Offline measurement should stabilize before adding online adaptation complexity. |
| Optuna | review soon, not added | Search over noisy/leaky objectives is not useful until the time-aware evaluation path is in regular use. |
| `statsforecast` / `darts` / endpoint forecasters | review soon, not added | Low current ROI versus baseline conditioning and pooled calibration work. |
