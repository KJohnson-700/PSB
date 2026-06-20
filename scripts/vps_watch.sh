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
# WSS price overlay: connected (real-time push mids) vs DOWN (REST-only, staler). Down = price/staleness risk.
# Current WSS state = the LAST websocket connect/subscribe/disconnect EVENT (not a fixed
# line window — the Connected line scrolls off, causing false DOWN). Subscribed/Connected=up.
WSS_LAST=$(grep -E "src\.market\.websocket" "$BOTLOG" 2>/dev/null | grep -iE "Connected to Polymarket|Subscribed to market for [0-9]+ token|Disconnected" | tail -1)
if echo "$WSS_LAST" | grep -qiE "Connected|Subscribed"; then WSS_STATE="connected"; elif echo "$WSS_LAST" | grep -qi "Disconnected"; then WSS_STATE="DOWN(REST-only)"; else WSS_STATE="?"; fi
# Price freshness: age (s) of newest /midpoint REST fetch vs now — real staleness signal
PRICE_TS=$(grep "midpoint" "$BOTLOG" 2>/dev/null | tail -1 | grep -oE "^[0-9-]+ [0-9:]+")
PRICE_AGE=$(python3 - "$PRICE_TS" <<'PY'
import sys,datetime
try:
    t=datetime.datetime.strptime(sys.argv[1],"%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    print(int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds()))
except Exception: print(-1)
PY
)
GEOBLOCK_RECENT=$(tail -2000 "$BOTLOG" 2>/dev/null | grep -cE "HTTP/[0-9.]+ 451|451 Client Error|403 Client Error|HTTP 403|[Ff]orbidden|[Gg]eo.?block")

# --- dashboard responsiveness (freeze/load regression) ---
DASH_CODE="?"; DASH_T="?"
if command -v curl >/dev/null; then
  read -r DASH_CODE DASH_T <<<"$(curl -s -o /dev/null -m 8 -w '%{http_code} %{time_total}' http://127.0.0.1:8082/api/scanner/health 2>/dev/null || echo 'TIMEOUT 8')"
fi

# --- RESTART/DEATH DETECTION + FORENSIC CAPTURE ---
# The whole point: catch a restart the instant it happens and snapshot WHY, so the
# cause can't vanish behind a relaunch. State persisted between runs.
SESSION_ID=$(python3 - "$STATUS" <<'PY'
import sys,json
try: print(json.load(open(sys.argv[1])).get('session_id','?'))
except Exception: print('?')
PY
)
MAIN_START=$(systemctl show psb-bot -p ExecMainStartTimestamp --value 2>/dev/null)
STATE_FILE=/home/ubuntu/.psb_watch_state
FORENSICS=/home/ubuntu/psb/data/logs/psb_restart_forensics.log
RESTART_EVENT=""
PREV_SESSION=""; PREV_START=""
[ -f "$STATE_FILE" ] && { PREV_SESSION=$(sed -n 1p "$STATE_FILE"); PREV_START=$(sed -n 2p "$STATE_FILE"); }
if [ -n "$PREV_START" ] && [ "$MAIN_START" != "$PREV_START" ]; then
  RESTART_EVENT="RESTART_DETECTED prev_session=$PREV_SESSION new_session=$SESSION_ID prev_start='$PREV_START' new_start='$MAIN_START'"
  {
    echo "=================================================================="
    echo "[$(date -u +%FT%TZ)] $RESTART_EVENT"
    echo "--- death marker (why the OLD process died) ---"; cat /home/ubuntu/psb/data/runtime/bot_death_marker.json 2>/dev/null || echo "  (none — clear marker = hard SIGKILL/OOM, or new proc cleared it)"
    echo "--- last 12 NON-INFO polybot lines before restart ---"; grep -ivE "INFO|DEBUG" /home/ubuntu/psb/data/logs/polybot.log 2>/dev/null | tail -12 | cut -c1-160
    echo "--- sudo commands in last 5 min (systemctl etc.) ---"; sudo grep -E "$(date -u +%Y-%m-%dT%H):" /var/log/auth.log 2>/dev/null | grep -iE "sudo:|systemctl" | tail -8
    echo "--- SSH sessions in last 5 min (who connected) ---"; sudo grep -E "$(date -u +%Y-%m-%dT%H):" /var/log/auth.log 2>/dev/null | grep -iE "Accepted|session opened" | tail -8
    echo "--- systemd: what initiated the stop ---"; journalctl -u psb-bot --since "10 min ago" --no-pager 2>/dev/null | grep -iE "Stopping|Stopped|Deactivated|Started|JOB_TYPE|signal|SIGTERM" | tail -10
  } >> "$FORENSICS" 2>&1
fi
printf "%s\n%s\n" "$SESSION_ID" "$MAIN_START" > "$STATE_FILE"

# --- last Hermes priority verdict ---
LAST_HEALTH=$(tail -1 "$HEALTH" 2>/dev/null)

# --- TRADE QUALITY: entry sanity + outcome streak (the data the infra watch lacked) ---
# Catches what "wss=connected / price_age ok" cannot: lanes entering at the blind
# 0.5 default (phantom edge, e.g. HYPE), losing streaks, and a collapsing WR — the
# trade-level signals, not just heartbeat. Reads the LIVE session's trades.
read -r BLIND05 LOSS_STREAK SESS_WR CLOSED_N BLIND_LANES <<<"$(python3 - "$PSB/data/calibration/trades.jsonl" "$SESSION_ID" <<'PY'
import json,sys
from collections import Counter
path,sess=sys.argv[1],sys.argv[2]
rows=[]
try:
    with open(path) as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try: d=json.loads(line)
            except: continue
            if d.get("session_id")==sess: rows.append(d)
except Exception:
    print("0 0 - 0 -"); sys.exit()
def is05(r):
    try: return abs(float(r.get("entry_price"))-0.5)<0.001
    except: return False
blind=[r for r in rows if is05(r)]
closed=[r for r in rows if r.get("pnl") is not None]
streak=0
for r in reversed(closed):
    if r.get("win"): break
    streak+=1
wr=int(round(100.0*sum(1 for r in closed if r.get("win"))/len(closed))) if closed else "-"
bl=Counter((r.get("strategy") or "?") for r in blind)
lanes=",".join("%s:%d"%(k,v) for k,v in bl.most_common(4)) or "-"
print(len(blind), streak, wr, len(closed), lanes)
PY
)"

# --- PRIORITY verdict ---
PRI=ok; REASONS=""
[ "${PROC_RSS_MB:-0}" -gt 800 ] && PRI=RSS_HIGH && REASONS="$REASONS rss=${PROC_RSS_MB}MB(>800=LEAK?)"
[ -z "$PID" ] && PRI=DOWN && REASONS="$REASONS no-main-proc(Restart=no: stays dead — read $FORENSICS)"
[ -n "$RESTART_EVENT" ] && PRI=RESTARTED && REASONS="$REASONS $RESTART_EVENT (forensics->$FORENSICS)"
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
# Price staleness: REST mids older than 30s = the bot is trading on stale prices (timing/price risk)
[ "${PRICE_AGE:-0}" -gt 30 ] && PRI=PRICE_STALE && REASONS="$REASONS price_age=${PRICE_AGE}s wss=$WSS_STATE"
# TRADE-QUALITY priorities (money, not infra) — placed last so they outrank heartbeat warnings.
if [ "${CLOSED_N:-0}" -ge 6 ] && [ "$SESS_WR" != "-" ]; then
  [ "${SESS_WR:-100}" -lt 30 ] && PRI=LOW_WR && REASONS="$REASONS session_wr=${SESS_WR}% n=$CLOSED_N"
fi
[ "${LOSS_STREAK:-0}" -ge 4 ] && PRI=LOSS_STREAK && REASONS="$REASONS loss_streak=$LOSS_STREAK consecutive"
[ "${BLIND05:-0}" -gt 0 ] && PRI=BLIND_05 && REASONS="$REASONS blind_0.5_entries=$BLIND05 [$BLIND_LANES] = phantom-edge bleed (unhydrated/degenerate book)"

echo "=== PSB WATCH $(date -u +%H:%M:%SZ) ==="
echo "LIVENESS  pid=${PID:-NONE} mode=$MODE phase=$PHASE status_age=${STATUS_AGE}s rss=${PROC_RSS_MB}MB"
echo "TRADES    daily_trades=$TRADES open=$OPEN cycle_count=$CYCLES   [target 100]"
echo "TRADEQUAL blind_0.5=$BLIND05 [$BLIND_LANES]  loss_streak=$LOSS_STREAK  session_wr=${SESS_WR}%  closed=$CLOSED_N"
echo "SESSION   id=$SESSION_ID main_start=$MAIN_START${RESTART_EVENT:+   ⚠️ $RESTART_EVENT}"
echo "CYCLE     elapsed=${CYC_MS}ms scanner_sync=${SCAN_SYNC}ms scan_total=${SCAN_TOTAL}ms overrun=${OVERRUN}ms interval=${INTERVAL_MS}ms"
echo "LANES     $LANES"
echo "TIMING    exit_check=${EXIT_MS}ms resolution=${RESO_MS}ms last_entry_age=${LAST_ENTRY_AGE}s last_exit_age=${LAST_EXIT_AGE}s"
echo "STALENESS log_age=${LOG_AGE}s status_age=${STATUS_AGE}s   ERRORS(new events, 2k)=$ERRORS_RECENT  known_sse_nameerr=${KNOWN_SSE_ERR:-0}(fixed,staged)"
echo "FEEDS     oracle_basis=$ORACLE_BASIS_RECENT oracle_stale=$ORACLE_STALE_RECENT kline_fb=$KLINE_FB_RECENT geoblock=$GEOBLOCK_RECENT (recent 2k loglines)"
echo "PRICE     wss=$WSS_STATE  newest_mid_age=${PRICE_AGE}s  (wss DOWN = REST-only, bot acts on staler mids)"
echo "DASH      /api/scanner/health http=$DASH_CODE time=${DASH_T}s"
echo "HERMES    $LAST_HEALTH"
echo "PRIORITY  $PRI$REASONS"

# --- Hermes emission (run as the */10 cron with --emit): append health feed + Discord on priority ---
# Writes the SAME vps_health.jsonl the Mac watcher (psb_vps_watcher.py) tails, so the existing
# watcher -> psb_live_drain -> phone pipeline carries the FULL comprehensive sweep, not the subset.
if [ "${1:-}" = "--emit" ]; then
  printf '{"ts":"%s","priority":"%s","rss_mb":%s,"cycle_ms":%s,"scanner_sync_ms":%s,"log_age_s":%s,"status_age_s":%s,"daily_trades":%s,"blind05":%s,"loss_streak":%s,"session_wr":"%s","closed":%s,"errors":%s,"geoblock":%s,"wss":"%s","price_age_s":%s,"session":"%s","reasons":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$PRI" "${PROC_RSS_MB:-0}" "${CYC_MS:--1}" "${SCAN_SYNC:--1}" "${LOG_AGE:--1}" "${STATUS_AGE:--1}" "${TRADES:-0}" "${BLIND05:-0}" "${LOSS_STREAK:-0}" "${SESS_WR:--}" "${CLOSED_N:-0}" "${ERRORS_RECENT:-0}" "${GEOBLOCK_RECENT:-0}" "${WSS_STATE:-?}" "${PRICE_AGE:--1}" "$SESSION_ID" "$(echo "$REASONS" | tr -d '"' | cut -c1-180)" >> "$HEALTH"
  if [ "$PRI" != "ok" ]; then
    CD=/tmp/psb_watch_alert_cooldown; LAST=$(cat "$CD" 2>/dev/null || echo 0); NOWS=$(date +%s)
    if [ $((NOWS-LAST)) -gt 1800 ]; then
      WH=$(grep -E "^DISCORD_WEBHOOK_URL=" "$PSB/.env" 2>/dev/null | cut -d= -f2- | tr -d "\"'")
      [ -n "$WH" ] && curl -s -m 10 -H "Content-Type: application/json" \
        -d "{\"content\":\"🔴 PSB watch: $PRI — cycle ${CYC_MS}ms rss ${PROC_RSS_MB}MB trades ${TRADES} | ${REASONS}\"}" "$WH" >/dev/null
      echo "$NOWS" > "$CD"
    fi
  fi
fi
