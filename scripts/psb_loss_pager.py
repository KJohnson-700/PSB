#!/usr/bin/env python3
"""PSB loss pager — pages Telegram when the paper session bleeds.

Routing (operator 2026-08-08):
  - Drawdown + bot-dead → Oracle 2 channel (one-liner, low-noise)
  - Single big loss (>= BIG_LOSS) → Slim DM (wakes Claude / agent)

Pages on, with per-condition 20-min backoff:
  - total_pnl <= -PNL_ALERT          (drawdown; default -$20)  → Oracle 2
  - any NEW single closed trade <= -BIG_LOSS                     → Slim DM
  - bot process dead                                             → Oracle 2

Auto-detects the newest session dir + the running `main.py --paper` pid, so it
survives restarts without editing. Runs continuously. Launch as a nohup daemon:
  nohup .venv/bin/python scripts/psb_loss_pager.py > data/logs/loss_pager.log 2>&1 &
"""
import json, os, time, subprocess, glob, re

HERMES = "/Users/mainfolder/.local/bin/hermes"
CWD = "/Users/mainfolder/Documents/psb-main 1"
PAPER_DIR = f"{CWD}/data/paper_trades"
LOG = f"{CWD}/data/logs/loss_pager.log"

# Routing targets (operator 2026-08-08):
#   Oracle 2 gets the noise (drawdown, bot dead) — low-signal, scannable ticker
#   Slim DM gets the wake-ups (single big loss) — wakes Claude for diagnosis
ORACLE_TARGET = "telegram:-1004498748669"   # PSB Oracle 2
DM_TARGET = "telegram:8273896884"            # Slim DM (wakes agent)

PNL_ALERT = -20.0     # total_pnl (realized+unrealized) drawdown threshold
BIG_LOSS = -30.0      # STOP-LOSS BUST: any single closed trade at/below this. A
                      # -$30 loss on ~$70-80 stake is ~-40%+, past the -55% cap intent / any
                      # working stop, i.e. a gap-through the exit machinery failed to catch.
CHECK = 60            # seconds between checks
BACKOFF = 1200        # 20 min min gap per condition

last_page = {}
_seen_big = set()     # trade ids already paged, so we don't re-alert the same loss
_last_dd = 0.0        # last drawdown level alerted — only re-alert when materially worse


def log(m):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}\n")
    except Exception:
        pass


def page(key, msg, target):
    now = time.time()
    if now - last_page.get(key, 0) >= BACKOFF:
        try:
            subprocess.run([HERMES, "send", "--to", target, msg], timeout=30)
            log(f"PAGED {key} -> {target}: {msg}")
        except Exception as e:
            log(f"page-fail {key}: {e}")
        last_page[key] = now


def newest_session():
    ds = [d for d in glob.glob(f"{PAPER_DIR}/*") if os.path.isdir(d)]
    return max(ds, key=os.path.getmtime) if ds else None


def bot_pid():
    try:
        out = subprocess.run(["pgrep", "-f", "main.py --paper"],
                             capture_output=True, text=True).stdout.strip()
        return int(out.split()[0]) if out else None
    except Exception:
        return None


def big_losses(sess_dir):
    """Return new single-trade losses <= BIG_LOSS not yet paged."""
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
            if p <= BIG_LOSS and tid not in _seen_big:
                _seen_big.add(tid)
                out.append((p, e))
    except Exception as ex:
        log(f"big_losses err: {ex}")
    return out


log("loss-pager START")
# Seed _seen_big with existing losses so we only alert on NEW ones after launch.
_s0 = newest_session()
if _s0:
    big_losses(_s0)
    log(f"seeded seen-losses from {os.path.basename(_s0)}")

while True:
    try:
        pid = bot_pid()
        if pid is None:
            # Bot dead → Oracle 2 (one-liner, low-noise)
            page("botdead", "PSB BOT DEAD (no main.py --paper)", ORACLE_TARGET)
            time.sleep(CHECK)
            continue
        sess = newest_session()
        if not sess:
            time.sleep(CHECK)
            continue
        sf = f"{sess}/summary.json"
        sname = os.path.basename(sess)
        if os.path.exists(sf):
            d = json.load(open(sf))
            total = float(d.get("total_pnl", 0) or 0)
            real = float(d.get("realized_pnl", 0) or 0)
            unreal = float(d.get("unrealized_pnl", 0) or 0)
            # Drawdown → Oracle 2, ONE line, and ONLY when it gets materially worse
            # ($5+ below the last alerted low) — not every tick while flat-underwater.
            if total <= PNL_ALERT and total <= _last_dd - 5.0:
                _last_dd = total
                page("drawdown",
                     f"PSB {sname}: {total:+.2f} (r{real:+.2f}/u{unreal:+.2f})",
                     ORACLE_TARGET)
            elif total > PNL_ALERT:
                _last_dd = 0.0   # recovered above threshold — reset so the next real dip re-alerts
            # Big loss(es) → ONE coalesced DM (the wake heads-up; autowake does the fixing).
            # Shared key => a cascade of busts is a SINGLE clean DM, never one-per-trade.
            busts = big_losses(sess)
            if busts:
                wp, we = min(busts, key=lambda x: x[0])
                more = f" +{len(busts)-1} more" if len(busts) > 1 else ""
                page("bigloss_dm",
                     f"PSB bust {wp:+.2f} {we.get('strategy')} {we.get('action')}{more}"
                     f" — autowake on it. {sname}",
                     DM_TARGET)
            log(f"ok total={total:+.2f} real={real:+.2f} pid={pid} sess={sname}")
    except Exception as ex:
        log(f"check-error {ex}")
    time.sleep(CHECK)
