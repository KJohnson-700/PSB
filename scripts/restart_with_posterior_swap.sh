#!/bin/zsh
# 2026-07-20 SWEEP FIX 1 (operator GO, Codex GO): restart the bot with the optimized
# posterior state. The calibrator reads lane_posteriors.json ON INIT ONLY and rewrites
# it from memory on every settle — so the swap MUST happen while the bot is stopped,
# or the stale in-memory June state clobbers the new file.
# Staged artifact: lane_posteriors.vps_20260720.json = VPS current (clean_liveonly
# 07-13 purge + all 07-13->20 live-only learning incl. the +869 era). Codex: GO,
# atomic replace, keep June file as .bak, discard today's ~50 local settles.
set -e
cd "/Users/mainfolder/Documents/psb-main 1"

echo "[1/6] stopping bot..."
pkill -f "start.py" 2>/dev/null || true
sleep 1
pkill -f "src/main.py --paper" 2>/dev/null || true
sleep 4
if pgrep -f "src/main.py --paper" >/dev/null; then echo "bot still running — ABORT"; exit 1; fi

echo "[2/6] backing up June posterior state..."
cp data/calibration/lane_posteriors.json data/calibration/lane_posteriors.json.bak_june_ghosty_20260720

echo "[3/6] validating staged file..."
.venv/bin/python - <<'EOF'
import json, sys
d = json.load(open('data/calibration/lane_posteriors.vps_20260720.json'))
assert d.get('schema_version') == 1, 'schema mismatch'
assert len(d['lanes']) > 1500, 'suspiciously small'
print('  staged file valid: %d lanes' % len(d['lanes']))
EOF

echo "[4/6] atomic swap..."
cp data/calibration/lane_posteriors.vps_20260720.json data/calibration/.lane_posteriors.tmp
mv data/calibration/.lane_posteriors.tmp data/calibration/lane_posteriors.json

echo "[5/6] starting bot (with memory profiler armed)..."
mkdir -p data/logs
PSB_MEM_PROFILE=1 nohup .venv/bin/python start.py > data/logs/local_restart_postswap_20260720.out 2>&1 &
echo "  launched pid $!"

echo "[6/6] waiting for startup..."
sleep 25
pgrep -fl "main.py --paper" || { echo "BOT DID NOT START — check data/logs/local_restart_postswap_20260720.out"; exit 1; }
echo "DONE. Verify via dashboard + spot-check: btc|1h|up should calibrate ~0.30 not 1.0."
