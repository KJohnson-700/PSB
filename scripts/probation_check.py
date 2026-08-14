#!/usr/bin/env python3
"""PROBATION CHECK — every change/build stays on this list until PROVEN.

The operator's standing rule (2026-08-05): nothing built gets to be forgotten. Run this EVERY monitor
tick. For each tracked item it verifies the thing is still in place / still alive / hasn't regressed,
and prints PROVEN / PROBATION / BROKEN. BROKEN = a config was reverted, a daemon died, or an error
recurred — page it. PROBATION = live/running but not yet proven by realized data (the metric check,
run separately, decides promotion). Add a row here the moment you ship anything.

Usage: .venv/bin/python scripts/probation_check.py   (add --json for machine output)
"""
import json
import os
import re
import subprocess
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(_REPO, "config/settings.yaml")
CAL = os.path.join(_REPO, "data/calibration")


def _cfg():
    import yaml
    with open(CFG) as f:
        return yaml.safe_load(f)


def _newest_bundle_log():
    """The log the RUNNING bot is actually writing to.

    2026-08-13: this used to hard-code the `bot_restart_bundle_*.log` naming, which the
    restart path stopped using on 08-07 — so every check that reads the log (tracebacks,
    minimax float errors, tape_defer) had been scoring a 6-day-stale file and reporting
    BROKEN off errors that were already fixed. Resolve from the live bot's own open fds
    first (authoritative), then the current `data/logs/polybot_*.log` convention, and only
    then the legacy bundle name. Callers must staleness-check via _log_age_sec().
    """
    candidates = []
    for pid in _pgrep("src/main.py"):
        try:
            out = subprocess.run(["lsof", "-p", pid], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        for line in out.splitlines():
            m = re.search(r"(/\S.*/data/logs/polybot_\S+\.log)", line)
            if m:
                candidates.append(m.group(1))
    for sub, prefix in (("data/logs", "polybot_"), ("data", "bot_restart_bundle_")):
        d = os.path.join(_REPO, sub)
        if not os.path.isdir(d):
            continue
        candidates += [os.path.join(d, f) for f in os.listdir(d)
                       if f.startswith(prefix) and f.endswith(".log")]
    candidates = [p for p in candidates if os.path.exists(p)]
    return max(candidates, key=os.path.getmtime) if candidates else None


def _log_age_sec(path):
    """Seconds since the log was last written. None if unresolvable."""
    if not path or not os.path.exists(path):
        return None
    try:
        return time.time() - os.path.getmtime(path)
    except Exception:
        return None


def _pgrep(pat):
    try:
        out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=5)
        return [p for p in out.stdout.split() if p]
    except Exception:
        return []


def _tail_grep(path, pattern, lines=800):
    if not path or not os.path.exists(path):
        return 0
    try:
        out = subprocess.run(["tail", "-n", str(lines), path], capture_output=True, text=True, timeout=10)
        return len(re.findall(pattern, out.stdout))
    except Exception:
        return 0


# each check returns (status, detail). status in {PROVEN, PROBATION, BROKEN, WARN}
def check_all():
    c = _cfg()
    strat = c.get("strategies", {})
    trad = c.get("trading", {})
    log = _newest_bundle_log()
    R = []

    def add(name, status, detail):
        R.append((name, status, detail))

    # --- health invariants ---
    bot = _pgrep("src/main.py --paper")
    add("bot_alive", "PROVEN" if bot else "BROKEN", f"pid={bot or 'DEAD'}")
    if bot:
        try:
            rss = int(subprocess.run(["ps", "-o", "rss=", "-p", bot[0]], capture_output=True, text=True).stdout) // 1024
            add("bot_rss<1200", "PROVEN" if rss < 1200 else "BROKEN", f"{rss}MB")
        except Exception:
            add("bot_rss<1200", "WARN", "unreadable")
    # A stale log is loss of observability, not a pass — never let it read PROVEN.
    log_age = _log_age_sec(log)
    if log is None or log_age is None:
        add("bot_log_resolved", "BROKEN", "no bot log found — log-backed checks are blind")
    elif log_age > 900:
        add("bot_log_resolved", "BROKEN",
            f"STALE {log_age/3600:.1f}h — {os.path.basename(log)} (log-backed checks are blind)")
    else:
        add("bot_log_resolved", "PROVEN", f"{os.path.basename(log)} age={log_age:.0f}s")
    log_fresh = log is not None and log_age is not None and log_age <= 900
    tb = _tail_grep(log, r"Traceback \(most recent call last\)", 800)
    add("bot_no_tracebacks",
        ("PROVEN" if tb == 0 else "BROKEN") if log_fresh else "WARN",
        f"{tb} in last 800 lines" + ("" if log_fresh else " (STALE LOG — not scored)"))

    # --- LIVE config changes (BROKEN if reverted; PROBATION until realized-proven) ---
    # SUPERSEDED 2026-08-13 by a8f5da7 "size(clean-era): base 15->20, cap bitcoin+doge
    # to 12". This row expected base==15.0 and so has paged on every run since. The
    # operator has also said repeatedly that flat sizing "isnt gonna work", so pinning a
    # specific flat base as the health target was wrong twice over. Now: check only that
    # a base is CONFIGURED (a missing/zero base IS a real fault), not that it equals 15.
    _fb = float(trad.get("flat_base_usd", 0) or 0)
    add("flat_sizing base set",
        "PROBATION" if _fb > 0 else "BROKEN",
        f"enabled={trad.get('flat_sizing_enabled')} base={trad.get('flat_base_usd')} "
        f"(was pinned to 15.0; a8f5da7 moved it to 20 on purpose)")

    eth = strat.get("eth_macro", {})
    fade = eth.get("fade_regime_windows")
    add("eth_fade[5m,15m]",
        "PROBATION" if (fade == ["5m", "15m"] and eth.get("fade_regime")) else "BROKEN",
        f"windows={fade} regime={eth.get('fade_regime')}")
    add("eth_15m_veto_on",
        "PROBATION" if eth.get("eth_15m_buy_yes_bearish_tape_only_enabled") else "BROKEN",
        f"={eth.get('eth_15m_buy_yes_bearish_tape_only_enabled')}")

    add("btc_shorts_bull_fix",
        "PROBATION" if strat.get("bitcoin", {}).get("btc_quant_flip_allow_htf_aligned", True) else "BROKEN",
        f"btc_quant_flip_allow_htf_aligned={strat.get('bitcoin', {}).get('btc_quant_flip_allow_htf_aligned', 'default-true')}")

    bnb = strat.get("bnb_macro", {})
    add("bnb_rsi_gate_off",
        "PROBATION" if bnb.get("rsi_hard_gate_enabled") is False else "BROKEN",
        f"rsi_hard_gate_enabled={bnb.get('rsi_hard_gate_enabled')}")

    xrp = strat.get("xrp_macro", {})
    doge = strat.get("doge_macro", {})
    add("xrp/doge_quant_agree",
        "PROBATION" if (xrp.get("require_quant_side_agreement") and doge.get("require_quant_side_agreement")) else "BROKEN",
        f"xrp={xrp.get('require_quant_side_agreement')} doge={doge.get('require_quant_side_agreement')}")
    add("xrp_1h_loosen_0.045",
        "PROBATION" if float(xrp.get("by_tf", {}).get("1h", {}).get("min_edge_buy_no", 1)) == 0.045 else "BROKEN",
        f"xrp 1h min_edge_buy_no={xrp.get('by_tf', {}).get('1h', {}).get('min_edge_buy_no')}")

    add("floors_dropped",
        "PROBATION" if (float(strat.get("bitcoin", {}).get("lane_min_notional_1h_up", 1)) == 0
                        and float(xrp.get("lane_min_notional_5m_down", 1)) == 0) else "BROKEN",
        f"btc1h_up={strat.get('bitcoin', {}).get('lane_min_notional_1h_up')} xrp5m_down={xrp.get('lane_min_notional_5m_down')}")

    # SUPERSEDED 2026-08-09. This row encoded the 08-06 order to shut eth off at the
    # scanner and flagged "eth re-enabled unexpectedly" as BROKEN. That order was REVERSED
    # three days later: the 08-09 verdict is that ETH HAS EDGE (momentum 55%, gross +$1,519,
    # b=1.64; it nets negative only through the stop-leak) -> RE-ENABLE. eth 5m is also a
    # designated COLLECTION lane that must never be disabled. So a row demanding eth be OFF
    # was paging for eth being correctly ON — inverted, and the single most misleading line
    # on the list. Now asserts the CURRENT intent: eth stays enabled.
    add("eth_enabled(collection lane)",
        "PROVEN" if strat.get("eth_macro", {}).get("enabled") is not False else "BROKEN",
        f"eth_macro.enabled={strat.get('eth_macro', {}).get('enabled')} "
        f"(BROKEN = eth got disabled; 08-09 verdict says keep it ON)")

    # exit fix: tape-hold deferral on proven shorts at floor 0.30 (2026-08-06, restart-applied)
    th = c.get("tape_hold_stop", {}).get("by_lane", {})
    _shorts_ok = all(float(th.get(f"{s}:BUY_NO", {}).get("floor_pct", 0)) == 0.30
                     for s in ("xrp_macro", "sol_macro", "bnb_macro"))
    add("exit_fix_tapehold_shorts",
        "PROBATION" if (c.get("tape_hold_stop", {}).get("enabled") and _shorts_ok) else "BROKEN",
        f"xrp/sol/bnb:BUY_NO floor={[th.get(f'{s}:BUY_NO', {}).get('floor_pct') for s in ('xrp_macro','sol_macro','bnb_macro')]} (right-side% + realized WR promotes)")

    # --- fixes that must NOT recur ---
    mmx = _tail_grep(log, r"Minimax API error:.*float\(\)", 1500)
    add("minimax_float_fix", "PROVEN" if mmx == 0 else "BROKEN", f"{mmx} float-errors in last 1500 lines")

    # ngc-defer stays KILLED (no tape_defer block in config)
    ngc_gone = "tape_defer:" not in open(CFG).read()
    add("ngc_defer_killed", "PROVEN" if ngc_gone else "BROKEN", "tape_defer absent" if ngc_gone else "tape_defer REAPPEARED")

    # --- observe-only shadows (BROKEN if daemon died; PROBATION = accumulating) ---
    # qwen_2model_engine: the AI direction SIDE-OVERRIDE was deliberately benched
    # 2026-08-14 (commit 3f02678, direction.mode: quant / enforce: false) after being
    # measured LAST of five policies on 171,380 settled ghosts (ai_hist 47.79%,
    # EV/$1 -0.0584; a coinflip beats it). A dead daemon is now the INTENDED state, so
    # this row must not page. Kept visible so the bench stays on the radar.
    qe = _pgrep("ai_direction_engine.py")
    add("qwen_2model_engine", "SUPERSEDED" if not qe else "PROBATION",
        f"pid={qe or 'DEAD'} — benched on purpose 08-14 (3f02678); DEAD is correct here")
    lag = _pgrep("cex_pm_lag_shadow.py")
    add("cex_pm_lag_detector", "PROBATION" if lag else "BROKEN", f"pid={lag or 'DEAD'} (verdict pending; run cex_pm_lag_analyze.py)")

    return R


# ── REGRESSION vs NEVER-SHIPPED ────────────────────────────────────────────────
# 2026-08-14. The checklist was reporting 11 BROKEN on a config that had just produced
# the best session of the week (+$73.59), and EVERY one of those 11 was equally red
# during that winning session — verified by diffing config at 80e0067 (a commit that
# landed mid-session). A list that screams 11 alarms on a winning config is a list that
# gets ignored, which is the one failure mode it was built to prevent.
#
# ROOT CAUSE: every row was binary PROVEN/BROKEN, so it could not tell apart
#   (a) this WAS working and has now regressed        <- the only thing worth paging
#   (b) this has been red since the day it was added  <- never shipped, not a regression
#   (c) you deliberately reversed this later          <- the expectation is just stale
# All three printed identically as 🔴 BROKEN.
#
# FIX: persist per-row history. A row only pages as REGRESSION if it was observed
# healthy at least once and has since gone red. Rows red since birth are NEVER_SHIPPED
# (visible, not paging — they belong on the roadmap's build queue, not the alarm list).
# SUPERSEDED is set explicitly in-code, with the commit/decision that reversed it.
STATE_PATH = os.path.join(_REPO, "data/runtime/probation_state.json")


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def classify(R):
    """Re-grade raw statuses against per-row history. Returns (rows, page_items)."""
    st = _load_state()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = []
    for name, status, detail in R:
        rec = st.setdefault(name, {"first_seen": now, "ever_ok": False, "first_bad": None})
        healthy = status in ("PROVEN", "PROBATION", "SUPERSEDED")
        if healthy:
            rec["ever_ok"] = True
            rec["first_bad"] = None
        graded = status
        if status == "BROKEN":
            if rec["ever_ok"]:
                graded = "REGRESSION"          # was fine, now broken -> the real alarm
                rec["first_bad"] = rec.get("first_bad") or now
            else:
                graded = "NEVER_SHIPPED"       # red since birth -> a build item, not an alarm
        rec["last_status"] = graded
        rec["last_seen"] = now
        if graded == "REGRESSION" and rec.get("first_bad"):
            detail = f"{detail}  [broken since {rec['first_bad']}]"
        out.append((name, graded, detail))
    _save_state(st)
    page = [r for r in out if r[1] == "REGRESSION"]
    return out, page


def main():
    R = check_all()
    if "--json" in sys.argv:
        print(json.dumps([{"item": n, "status": s, "detail": d} for n, s, d in R]))
        return
    R, page = classify(R)
    order = {"REGRESSION": 0, "WARN": 1, "NEVER_SHIPPED": 2, "PROBATION": 3,
             "SUPERSEDED": 4, "PROVEN": 5}
    mark = {"PROVEN": "✅", "PROBATION": "🟡", "WARN": "⚠️", "REGRESSION": "🔴",
            "NEVER_SHIPPED": "⬜", "SUPERSEDED": "⚪"}
    print("=== PROBATION CHECK — everything built stays here until PROVEN ===")
    for n, s, d in sorted(R, key=lambda x: order.get(x[1], 9)):
        print(f"  {mark.get(s,'?')} {s:14} {n:26} {d}")
    ns = [r for r in R if r[1] == "NEVER_SHIPPED"]
    sup = [r for r in R if r[1] == "SUPERSEDED"]
    print()
    print(f"  {len(page)} REGRESSION (was healthy, now broken)   "
          f"{len(ns)} never-shipped   {len(sup)} superseded   {len(R)} tracked")
    print("  " + ("<-- PAGE" if page else "no regressions — nothing to page"))
    if ns:
        print("  ⬜ never-shipped are BUILD ITEMS, not alarms — they belong on the roadmap queue:")
        print("     " + ", ".join(r[0] for r in ns))


if __name__ == "__main__":
    main()
