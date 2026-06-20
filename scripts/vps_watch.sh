#!/usr/bin/env bash
# PSB consolidated trouble-area watch. One pass = all known failure modes.
# Run on VPS. Emits a single compact report + a PRIORITY verdict line.
set -uo pipefail
PSB=/home/ubuntu/psb
STATUS=$PSB/data/runtime/bot_runtime_status.json
HEALTH=$PSB/data/logs/vps_health.jsonl
BOTLOG=$PSB/data/logs/polybot.log
NOW=$(date -u +%s)

# --- bot process / liveness ---
PID=$(pgrep -f "main.py" | head -1)
PROC_RSS_KB=$(ps -o rss= -p "${PID:-0}" 2>/dev/null | tr -d ' ')
PROC_RSS_MB=$(( ${PROC_RSS_KB:-0} / 1024 ))

# --- status file freshness + core metrics ---
read -r STATUS_AGE TRADES OPEN CYCLES CYC_MS SCAN_SYNC SCAN_TOTAL OVERRUN PID_J MODE PHASE <<<"$(python3 - "$STATUS" <<'PY'
import sys,json,os,datetime
p=sys.argv[1]
try:
    d=json.load(open(p))
    ts=d.get('ts'); age='?'
    if ts:
        t=datetime.datetime.fromisoformat(ts)
        age=int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds())
    ct=d.get('cycle_timings_ms',{}) or {}
    print(age, d.get('daily_trades','?'), d.get('open_positions','?'),
          d.get('cycle_count','?'), ct.get('cycle_elapsed_ms','?'),
          ct.get('scanner_sync_ms','?'), ct.get('strategy_scan_total_ms','?'),
          ct.get('cycle_overrun_ms','?'), d.get('pid','?'),
          d.get('mode','?'), d.get('phase','?'))
except Exception as e:
    print('ERR','?','?','?','?','?','?','?','?','?','?')
PY
)"

# --- per-lane scan times (catch HYPE/Hyperliquid hang) ---
LANES=$(python3 - "$STATUS" <<'PY'
import sys,json
try:
    d=json.load(open(sys.argv[1]))
    m=(d.get('cycle_timings_ms',{}) or {}).get('strategy_scan_by_name_ms',{}) or {}
    print(" ".join(f"{k.split('_')[0]}={int(v)}" for k,v in sorted(m.items(), key=lambda x:-x[1])))
except Exception:
    print('?')
PY
)
SLOW_LANE_MS=$(echo "$LANES" | grep -oE '=[0-9]+' | tr -d '=' | sort -rn | head -1)

# --- log staleness (bot writing?) + errors + cadence/timing ---
LOG_AGE="?"
if [ -f "$BOTLOG" ]; then LOG_AGE=$(( NOW - $(stat -c %Y "$BOTLOG" 2>/dev/null || echo "$NOW") )); fi
# Count ERROR *events* via exception-type footers (not every traceback frame line, which
# multiplies one error into dozens). Exclude the KNOWN+FIXED _read_config_file NameError
# (commit a5cf0d6, staged to disk, clears on the 100-trade restart) so it can't mask NEW errors.
ERRORS_RECENT=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -E "^[A-Za-z_.]+(Error|Exception):|CRITICAL" | grep -vc "_read_config_file")
KNOWN_SSE_ERR=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -c "_read_config_file' is not defined")
# exit-loop + resolution timing from status (entry/exit cadence)
read -r EXIT_MS RESO_MS INTERVAL_MS <<<"$(python3 - "$STATUS" <<'PY'
import sys,json
try:
    ct=(json.load(open(sys.argv[1])).get('cycle_timings_ms',{}) or {})
    print(ct.get('cycle_exit_check_ms','?'), ct.get('resolution_check_ms','?'), ct.get('cycle_interval_ms','?'))
except Exception: print('?','?','?')
PY
)"
# time since last entry + last exit (trade cadence / entry+exit timing freshness)
read -r LAST_ENTRY_AGE LAST_EXIT_AGE <<<"$(python3 - <<PY
import json,os,datetime,glob
now=datetime.datetime.now(datetime.timezone.utc)
def age(paths,keys):
    last=None
    for p in paths:
        if not os.path.exists(p): continue
        try:
            with open(p) as f:
                for line in f.readlines()[-400:]:
                    try: d=json.loads(line)
                    except: continue
                    if any(k in d for k in keys):
                        t=d.get('ts') or d.get('timestamp') or d.get('time') or d.get('exit_ts') or d.get('entry_ts')
                        if not t: continue
                        try: dt=datetime.datetime.fromisoformat(str(t).replace('Z','+00:00'))
                        except: continue
                        if dt.tzinfo is None: dt=dt.replace(tzinfo=datetime.timezone.utc)
                        if last is None or dt>last: last=dt
        except: pass
    return int((now-last).total_seconds()) if last else -1
base="$PSB/data/calibration"
print(age([base+"/trades.jsonl"],["side","entry_price","yes_price"]),
      age([base+"/trades_settled.jsonl",base+"/trades.jsonl"],["exit_price","held_pnl","realized_pct"]))
PY
)"

# --- oracle staleness + kline fallback (price feed health, recent only) ---
ORACLE_STALE_RECENT=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -c -i "oracle.*stale\|oracle_stale" )
KLINE_FB_RECENT=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -c -i "kline_fallback\|kline.*fallback")
ORACLE_BASIS_RECENT=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -c -i "oracle_basis")
GEOBLOCK_RECENT=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -c -i "geoblock\|403\|cloudfront")

# --- dashboard responsiveness (freeze/load regression) ---
DASH_CODE="?"; DASH_T="?"
if command -v curl >/dev/null; then
  read -r DASH_CODE DASH_T <<<"$(curl -s -o /dev/null -m 8 -w '%{http_code} %{time_total}' http://127.0.0.1:8082/api/scanner/health 2>/dev/null || echo 'TIMEOUT 8')"
fi

# --- last Hermes priority verdict ---
LAST_HEALTH=$(tail -1 "$HEALTH" 2>/dev/null)

# --- PRIORITY verdict ---
PRI=ok; REASONS=""
[ "${PROC_RSS_MB:-0}" -gt 800 ] && PRI=RSS_HIGH && REASONS="$REASONS rss=${PROC_RSS_MB}MB(>800=LEAK?)"
[ -z "$PID" ] && PRI=DOWN && REASONS="$REASONS no-main-proc"
if [ "$STATUS_AGE" != "?" ] && [ "$STATUS_AGE" != "ERR" ]; then
  [ "$STATUS_AGE" -gt 180 ] && PRI=STALE_STATUS && REASONS="$REASONS status_age=${STATUS_AGE}s"
fi
if [ "$CYC_MS" != "?" ]; then
  [ "${CYC_MS%.*}" -gt 20000 ] && PRI=CYCLE_LAG && REASONS="$REASONS cycle=${CYC_MS}ms"
fi
[ -n "${SLOW_LANE_MS:-}" ] && [ "${SLOW_LANE_MS:-0}" -gt 20000 ] && PRI=LANE_HANG && REASONS="$REASONS slow_lane=${SLOW_LANE_MS}ms"
[ "$DASH_CODE" != "200" ] && [ "$DASH_CODE" != "?" ] && PRI=DASH_DOWN && REASONS="$REASONS dash=$DASH_CODE/${DASH_T}s"
[ "${GEOBLOCK_RECENT:-0}" -gt 5 ] && PRI=GEOBLOCK && REASONS="$REASONS geoblock=$GEOBLOCK_RECENT"
[ "$LOG_AGE" != "?" ] && [ "${LOG_AGE:-0}" -gt 180 ] && PRI=STALE_LOG && REASONS="$REASONS log_age=${LOG_AGE}s"
[ "${ERRORS_RECENT:-0}" -gt 20 ] && PRI=ERRORS && REASONS="$REASONS errors=$ERRORS_RECENT"
if [ "$OVERRUN" != "?" ] && [ "${OVERRUN%.*}" -gt 5000 ]; then PRI=CADENCE && REASONS="$REASONS overrun=${OVERRUN}ms"; fi

echo "=== PSB WATCH $(date -u +%H:%M:%SZ) ==="
echo "LIVENESS  pid=${PID:-NONE} mode=$MODE phase=$PHASE status_age=${STATUS_AGE}s rss=${PROC_RSS_MB}MB"
echo "TRADES    daily_trades=$TRADES open=$OPEN cycle_count=$CYCLES   [target 100]"
echo "CYCLE     elapsed=${CYC_MS}ms scanner_sync=${SCAN_SYNC}ms scan_total=${SCAN_TOTAL}ms overrun=${OVERRUN}ms interval=${INTERVAL_MS}ms"
echo "LANES     $LANES"
echo "TIMING    exit_check=${EXIT_MS}ms resolution=${RESO_MS}ms last_entry_age=${LAST_ENTRY_AGE}s last_exit_age=${LAST_EXIT_AGE}s"
echo "STALENESS log_age=${LOG_AGE}s status_age=${STATUS_AGE}s   ERRORS(new events, 2k)=$ERRORS_RECENT  known_sse_nameerr=${KNOWN_SSE_ERR:-0}(fixed,staged)"
echo "FEEDS     oracle_basis=$ORACLE_BASIS_RECENT oracle_stale=$ORACLE_STALE_RECENT kline_fb=$KLINE_FB_RECENT geoblock=$GEOBLOCK_RECENT (recent 2k loglines)"
echo "DASH      /api/scanner/health http=$DASH_CODE time=${DASH_T}s"
echo "HERMES    $LAST_HEALTH"
echo "PRIORITY  $PRI$REASONS"
