#!/usr/bin/env python3
"""PSB auto-wake — a stop-loss BUST spawns a headless Claude that FIXES it.

Operator 2026-08-08: "if a trade busts through our stop, have Hermes wake you up
and you address it — you have authorization to fix the bug as long as it's Codex
reviewed, then hot-reload if you can, else restart the session."

This is the real wake-link: the alert pager pings the phone; THIS daemon actually
launches a Claude Code agent (headless `claude -p`) to diagnose + fix the bust,
autonomously, under the standing authorization. Guardrails:
  - fires ONLY on a NEW single closed trade <= BUST_USD (default -$30)
  - debounce: at most one wake per DEBOUNCE_SEC (default 30 min) — no cascade spam
  - each spawned agent has a hard timeout (WAKE_TIMEOUT_SEC)
  - the agent prompt hard-constrains it: --paper ONLY, Codex-review before any code
    change, back up before edits, report to Oracle 2. It fixes ONE thing then stops.

Launch as a nohup daemon:
  nohup .venv/bin/python scripts/psb_autowake.py > data/logs/autowake.log 2>&1 &
"""
import json, os, time, subprocess, glob

CLAUDE = "/opt/homebrew/bin/claude"
HERMES = "/Users/mainfolder/.local/bin/hermes"
CWD = "/Users/mainfolder/Documents/psb-main 1"
PAPER_DIR = f"{CWD}/data/paper_trades"
LOG = f"{CWD}/data/logs/autowake.log"
ORACLE_TARGET = "telegram:-1004498748669"   # PSB Oracle 2 (status of the wake)

BUST_USD = -30.0          # single-trade loss that triggers a wake (matches pager BIG_LOSS)
DEBOUNCE_SEC = 1800       # >= 30 min between wakes (a fix session needs room to run)
CHECK = 60                # poll interval
WAKE_TIMEOUT_SEC = 1200   # hard cap on a spawned fix agent (20 min)

_seen = set()
_last_wake = 0.0


def log(m):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}\n")
    except Exception:
        pass


def notify(msg):
    try:
        subprocess.run([HERMES, "send", "--to", ORACLE_TARGET, msg], timeout=30)
    except Exception as e:
        log(f"notify-fail {e}")


def newest_session():
    ds = [d for d in glob.glob(f"{PAPER_DIR}/*") if os.path.isdir(d)]
    return max(ds, key=os.path.getmtime) if ds else None


def new_busts(sess_dir):
    ef = f"{sess_dir}/entries.jsonl"
    out = []
    if not os.path.exists(ef):
        return out
    try:
        for line in open(ef):
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            p = e.get("pnl")
            if p is None:
                continue
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            tid = e.get("trade_id") or f"{e.get('market_id')}:{e.get('timestamp')}"
            if p <= BUST_USD and tid not in _seen:
                _seen.add(tid)
                out.append((p, e))
    except Exception as ex:
        log(f"scan-err {ex}")
    return out


def wake_prompt(p, e, sname):
    return (
        f"AUTO-WAKE — PSB stop-loss BUST. A single PAPER trade just lost ${p:.2f} "
        f"(past our stop/cap): lane={e.get('strategy')} {e.get('action')} "
        f"window={(e.get('extra') or {}).get('window_size')} entry={e.get('entry_price')} "
        f"reason={e.get('reason')} market='{str(e.get('market_question'))[:40]}' session={sname}.\n\n"
        "You have STANDING AUTHORIZATION from the operator to fix this autonomously NOW:\n"
        "1) Diagnose WHY the cap/de-risk/exit machinery failed to catch this loss (pull the "
        "trade from data/paper_trades/" + sname + "/entries.jsonl + the newest data/logs/"
        "bot_restart_*.log; check the exit path in src/execution/live_testing.py).\n"
        "2) Codex-review the fix: ~/.hermes/node/bin/codex . Do NOT ship a code change Codex "
        "flags as a NO-GO without resolving it.\n"
        "3) Apply it: if hot-reloadable, `touch data/reload_code.flag`; else restart the paper "
        "session (SIGTERM the `main.py --paper` pid, then "
        "`nohup .venv/bin/python src/main.py --paper > data/logs/bot_restart_$(date +%Y%m%d_%H%M%S).log 2>&1 &`).\n\n"
        "HARD RULES: --paper ONLY, NEVER --live. Back up config before any edit "
        "(cp config/settings.yaml config/settings.yaml.bak_autowake_$(date +%s)). Fix ONLY this "
        "bust class, do not refactor. When done, send a 1-paragraph report of what you found + "
        f"did via: {HERMES} send --to {ORACLE_TARGET} \"AUTOWAKE FIX: ...\". Then stop.\n"
        f"Repo/cwd: {CWD}"
    )


def spawn_fix(p, e, sname):
    prompt = wake_prompt(p, e, sname)
    logf = f"{CWD}/data/logs/autowake_run_{time.strftime('%Y%m%d_%H%M%S')}.log"
    log(f"WAKE spawning claude for bust {p:+.2f} {e.get('strategy')} -> {logf}")
    notify(f"PSB AUTO-WAKE: bust {p:+.2f} {e.get('strategy')} {e.get('action')} — spawning Claude to diagnose+fix (Codex-reviewed). sess {sname}")
    try:
        with open(logf, "w") as out:
            subprocess.run(
                [CLAUDE, "-p", prompt, "--dangerously-skip-permissions"],
                cwd=CWD, stdout=out, stderr=subprocess.STDOUT,
                timeout=WAKE_TIMEOUT_SEC,
            )
        log(f"WAKE agent completed -> {logf}")
    except subprocess.TimeoutExpired:
        log(f"WAKE agent TIMEOUT ({WAKE_TIMEOUT_SEC}s)")
        notify(f"PSB AUTO-WAKE: fix agent hit {WAKE_TIMEOUT_SEC}s timeout — check {os.path.basename(logf)}")
    except Exception as ex:
        log(f"WAKE spawn-err {ex}")
        notify(f"PSB AUTO-WAKE: spawn failed ({ex}) — Claude not launched, handle manually")


log("autowake START")
_s0 = newest_session()
if _s0:
    new_busts(_s0)   # seed: only wake on busts AFTER launch
    log(f"seeded from {os.path.basename(_s0)}")

while True:
    try:
        sess = newest_session()
        if sess:
            for p, e in new_busts(sess):
                now = time.time()
                if now - _last_wake < DEBOUNCE_SEC:
                    log(f"bust {p:+.2f} but DEBOUNCED ({int(now - _last_wake)}s < {DEBOUNCE_SEC}) — logged, not spawning")
                    notify(f"PSB bust {p:+.2f} {e.get('strategy')} — auto-wake debounced (a fix is recent). Check if needed.")
                    continue
                _last_wake = now
                spawn_fix(p, e, os.path.basename(sess))
    except Exception as ex:
        log(f"loop-err {ex}")
    time.sleep(CHECK)
