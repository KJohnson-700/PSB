## 2026-05-28 — Calibration recommendation anchor

**Context:** Operator flagged drift risk after a good paper test (`test_20260527_042014`: 203 closed, +154.90) followed by later blockage/weak throughput (`test_20260527_174918`: 17 closed, -4.92). Current project phase from `CLAUDE.md` is calibration/data gathering: trade frequency and truthful skip taxonomy matter more than adding restrictions.

**Current recommendation:** Preserve the high-throughput calibration posture from the good run. Treat loss-pause/manual-resume behavior, broad blocked UTC hours, and legacy/aggregated skip labels as calibration blockers until proven protective by settled ghosts or live journal evidence.

**Immediate priorities:**
- Restore paper loss-pause auto-resume behavior unless manually overridden; paper calibration should not become session-stuck after a lane pause.
- Keep lane management advisory-only while calibrating unless the operator explicitly flips execution enforcement.
- Clean skip-reason semantics: remove or version legacy reasons, split composite `lane_min_edge`, and stop treating diagnostic-only `diag_*` markers as gates.
- Prefer soft penalties or telemetry for alt BTC-follow/correlation gates; BTC should not decide alt admission.
- Make future changes one at a time and compare against the strong `test_20260527_042014` run plus settled ghost slices.

**Do not drift into:** broad tightening, global min-edge raises, narrower entry windows, or assuming zero trades means success.
