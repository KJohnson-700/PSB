#!/usr/bin/env bash
# REVERT the 2026-08-15 trial: strategies.xrp_macro.lane_max_notional_15m_up  40 -> 10
#
# Safe to run repeatedly. Hot-reloads (sol_macro.py:7395 reads it via self.config.get()
# per call, and `strategies` is in _HOT_RELOAD_TOP_LEVEL_KEYS) — NO restart required.
# Reverts ONLY this one key; every other change in the working tree is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."

CUR=$(grep -c '^    lane_max_notional_15m_up: 40' config/settings.yaml || true)
if [ "$CUR" -eq 0 ]; then
  echo "nothing to revert — lane_max_notional_15m_up is not at 40:"
  grep -n '^    lane_max_notional_15m_up:' config/settings.yaml
  exit 0
fi

cp config/settings.yaml "config/settings.yaml.bak_pre_REVERT_xrp15m_$(date +%H%M%S)"
/usr/bin/sed -i '' 's/^    lane_max_notional_15m_up: 40$/    lane_max_notional_15m_up: 10/' config/settings.yaml

.venv/bin/python - <<'PY'
import sys, yaml
cfg = yaml.safe_load(open('config/settings.yaml'))
v = cfg['strategies']['xrp_macro']['lane_max_notional_15m_up']
print("YAML parses OK; lane_max_notional_15m_up = %s" % v)
sys.exit(0 if v == 10 else 1)
PY

echo "REVERTED to 10. Hot-reload picks it up within ~1 scan cycle (no restart)."
echo "Confirm with:  grep 'adaptive-sizer:live.*xrp_macro 15m BUY_YES' \$(ls -t logs/psb_*.log | head -1) | tail -3"
