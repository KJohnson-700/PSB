#!/usr/bin/env python3
"""PSB token-save Hermes watcher — runs until noon PT, pages telegram on threshold.
Claude loop is OFF (token-save); Hermes owns paging (does NOT depend on Claude being awake).
Pages: realized<=-30, any lane<=-45, bot death, RSS>1200MB. Stateful 30min backoff per condition.
Launched as a nohup daemon (launchd can't touch ~/Documents on Sequoia). Self-exits at noon PT.
"""
import json, os, time, subprocess, glob, re
from datetime import datetime

HERMES = "/Users/mainfolder/.local/bin/hermes"
CWD = "/Users/mainfolder/Documents/psb-main 1"
SESS = "test_20260805_120550"
BOTPID = 74565
SUMMARY = f"{CWD}/data/paper_trades/{SESS}/summary.json"
LOG = f"{CWD}/data/hermes_watch.log"
BOTLOG_GLOB = f"{CWD}/data/bot_restart_*.log"  # 2026-08-05: newest bot stdout log for feed-staleness
STOP_HOUR = 23          # 11pm PT (machine local tz = PT); re-arm for overnight if needed
CHECK = 420             # 7 min between checks
BACKOFF = 1800          # 30 min min gap per condition

last_page = {}
_state = {"silence_seen": 0, "oracle_seen": 0}  # 2026-08-05 feed-staleness: WS silence + oracle counters last tick

def newest_botlog():
    fs = glob.glob(BOTLOG_GLOB)
    return max(fs, key=os.path.getmtime) if fs else None

def feed_staleness():
    """Return (should_page, msg). Genuine stale feed = WS went fully SILENT (silence_watchdog
    force-reconnect) OR WS marks 'used' collapsed to ~0. The benign ~REST-fallback stale count
    is NOT paged (bot self-protects via price_max_age_sec=8). Tail-based, cheap."""
    lf = newest_botlog()
    if not lf:
        return False, ""
    try:
        # cheap tail: read last ~200KB
        with open(lf, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 600_000))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return False, ""
    sil = tail.count("WS_SILENCE_WATCHDOG")
    delta = sil - _state.get("silence_seen", 0)
    _state["silence_seen"] = sil
    # latest WS_MARK_STATS used/stale
    used = None
    for m in re.finditer(r"WS_MARK_STATS\s+\{[^}]*'used':\s*(\d+)", tail):
        used = int(m.group(1))
    if delta >= 1:
        return True, f"PSB FEED STALE: WS_SILENCE_WATCHDOG fired {delta}x since last check (feed went fully silent >120s, force-reconnected). used_marks={used}. Bot self-protects via price_max_age=8 (REST fallback) but investigate feed."
    if used is not None and used == 0:
        return True, f"PSB FEED STALE: WS marks used=0 (WS fully dead, all REST). Trades still gated by price_max_age=8 but WS is down."
    # 2026-08-05: TAPE MAP staleness — tape_map.jsonl should update ~every 60s scan cycle.
    tf = f"{CWD}/data/calibration/tape_map.jsonl"
    try:
        if os.path.exists(tf):
            tape_age = time.time() - os.path.getmtime(tf)
            if tape_age > 360:  # >6min = tape feed frozen (klines/scan stalled)
                return True, f"PSB TAPE STALE: tape_map.jsonl not updated for {tape_age:.0f}s (>360). Direction/tape gates riding a frozen map — scan or kline fetch stalled."
    except Exception:
        pass
    # ORACLE staleness firing in the bot log tail
    if "oracle_stale" in tail or "TAPE_STALE" in tail or "geoblock" in tail:
        oc = tail.count("oracle_stale") + tail.count("geoblock")
        d2 = oc - _state.get("oracle_seen", 0)
        _state["oracle_seen"] = oc
        if d2 >= 5:  # sustained oracle staleness, not a one-off
            return True, f"PSB ORACLE STALE: {d2} oracle_stale/geoblock events since last check — basis/settlement price source degraded."
    return False, ""

def log(m):
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.now().isoformat()} {m}\n")
    except Exception:
        pass

def page(key, msg):
    now = time.time()
    if now - last_page.get(key, 0) >= BACKOFF:
        try:
            subprocess.run([HERMES, "send", "--to", "telegram", msg], timeout=30)
        except Exception as e:
            log(f"page-fail {e}")
        last_page[key] = now
        log(f"PAGED {key}: {msg}")

def alive(pid):
    try:
        os.kill(pid, 0); return True
    except Exception:
        return False

log(f"hermes-watch START sess={SESS} pid={BOTPID} until noon PT")
while True:
    if datetime.now().hour >= STOP_HOUR:
        try:
            subprocess.run([HERMES, "send", "--to", "telegram",
                            f"PSB hermes-watch ended (noon PT). sess {SESS}"], timeout=30)
        except Exception:
            pass
        log("STOP noon PT")
        break
    try:
        if not alive(BOTPID):
            page("botdead", f"PSB BOT DEAD pid {BOTPID} sess {SESS} - Claude in token-save, check bot")
        else:
            rss = 0
            try:
                rss = int((subprocess.run(["ps", "-o", "rss=", "-p", str(BOTPID)],
                                          capture_output=True, text=True).stdout or "0").strip() or 0) // 1024
            except Exception:
                pass
            if rss > 1200:
                page("rss", f"PSB RSS {rss}MB >1200 pid {BOTPID} sess {SESS}")
            d = json.load(open(SUMMARY))
            real = float(d.get("realized_pnl", 0) or 0)
            ss = d.get("strategy_stats", {}) or {}
            worst = min(ss.items(), key=lambda kv: kv[1]["pnl"]) if ss else None
            if real <= -30:
                wtxt = f" worst {worst[0]} {worst[1]['pnl']:+.2f}" if worst else ""
                page("realized", f"PSB realized {real:+.2f} <=-30 sess {SESS}{wtxt}")
            if worst and float(worst[1]["pnl"]) <= -45:
                page("lane", f"PSB lane {worst[0]} {worst[1]['pnl']:+.2f} <=-45 sess {SESS}")
            wtag = f" worst={worst[0]} {worst[1]['pnl']:+.2f}" if worst else ""
            # 2026-08-05 FEED-STALENESS check (the gap that let staleness go unpaged)
            try:
                stale, smsg = feed_staleness()
                if stale:
                    page("feedstale", smsg)
                    log(f"PAGED feedstale: {smsg[:80]}")
            except Exception as fe:
                log(f"feedstale-error {fe}")
            log(f"ok real={real:+.2f} rss={rss}MB{wtag} sil={_state.get('silence_seen')}")
    except Exception as e:
        log(f"check-error {e}")
    time.sleep(CHECK)
