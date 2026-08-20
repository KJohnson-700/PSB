#!/bin/bash
# REVERT PACKAGE — restore the 08-08 +$244 WINNING BASELINE config (operator order 2026-08-19).
# Config-only: code stays at current HEAD. Prepared by Claude; run on operator GO.
#
# What this does:
#   1. Backs up the live config to config/settings.yaml.bak_pre_244revert_<ts>
#   2. Copies config/settings.yaml.WINNING_BASELINE_plus244_20260808_1637 into place
#   3. Applies TWO compat patches (behavior-equivalence, documented below)
#   4. Touches data/reload_code.flag (hot keys) — the RESTART to fully apply is a
#      SEPARATE manual step (operator-gated):
#      launchctl kickstart -k gui/$(id -u)/com.psb.codex.paper
#
# Compat patches (delete the python block to restore raw bytes instead):
#   a. favorite_lane.respect_ai_direction -> false. On 08-08 this was true AND the AI
#      direction engine was alive, so favorites fired. The engine is benched today; true
#      on today's code = favorites sit out permanently (the 08-14 strangulation bug).
#      false reproduces the 08-08 BEHAVIOR.
#   b. lane_management.states: re-apply the current deliberate lane pauses (baseline has
#      none). Standing rule: a baseline restore never reverts deliberate disables.
#      Operator can empty this list to bring all lanes back.
#
# Known reversions this restore accepts ON PURPOSE (authentic to the +244 state):
#   - entry_admission_calibration_shrink back to 0.28 (blanket)
#   - portfolio halt ladder OFF (key absent -> disabled)
#   - RTDS oracle OFF, strict fresh-fill OFF (keys absent)
#   - 08-08-era exit stack (pre "exits killed", pre TP 0.55 / giveback 0.55 / depth restore)
# Honest binary settles SURVIVE (code default updown_expiry_grace_mins=10).
set -euo pipefail
cd "$(dirname "$0")/.."
TS=$(date +%H%M%S)
BASE=config/settings.yaml.WINNING_BASELINE_plus244_20260808_1637
[ -f "$BASE" ] || { echo "baseline file missing: $BASE"; exit 1; }
cp config/settings.yaml "config/settings.yaml.bak_pre_244revert_${TS}"
echo "backed up live config -> config/settings.yaml.bak_pre_244revert_${TS}"
cp "$BASE" config/settings.yaml

python3 - "config/settings.yaml.bak_pre_244revert_${TS}" <<'EOF'
import sys, yaml
prev = yaml.safe_load(open(sys.argv[1]))          # the config we just replaced
cfg  = yaml.safe_load(open("config/settings.yaml"))
# (a) favorites must be able to fire with the direction engine benched
cfg.setdefault("favorite_lane", {})["respect_ai_direction"] = False
# (b) preserve deliberate lane pauses
pauses = (prev.get("lane_management") or {}).get("states") or {}
if pauses:
    cfg.setdefault("lane_management", {})["states"] = pauses
assert cfg.get("trading", {}).get("dry_run") is True, "dry_run must stay true"
yaml.safe_dump(cfg, open("config/settings.yaml", "w"), sort_keys=False, width=120)
print(f"compat patches applied: respect_ai_direction=false, {len(pauses)} lane pauses preserved")
EOF

touch data/reload_code.flag
echo "DONE. Config is the +244 baseline (+2 compat patches). Restart to fully apply:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.psb.codex.paper"
