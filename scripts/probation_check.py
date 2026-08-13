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
    add("flat_sizing $15",
        "PROBATION" if (trad.get("flat_sizing_enabled") and float(trad.get("flat_base_usd", 0)) == 15.0) else "BROKEN",
        f"enabled={trad.get('flat_sizing_enabled')} base={trad.get('flat_base_usd')}")

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

    # eth shut down at the scanner (operator order 2026-08-06; worst-direction 43% + over-scanned)
    add("eth_scanner_off",
        "PROBATION" if strat.get("eth_macro", {}).get("enabled") is False else "BROKEN",
        f"eth_macro.enabled={strat.get('eth_macro', {}).get('enabled')} (BROKEN=eth re-enabled unexpectedly)")

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
    qe = _pgrep("ai_direction_engine.py")
    add("qwen_2model_engine", "PROBATION" if qe else "BROKEN", f"pid={qe or 'DEAD'} (right-side% vs realized pending; Hermes: format/vision ok, math weak=NA)")
    lag = _pgrep("cex_pm_lag_shadow.py")
    add("cex_pm_lag_detector", "PROBATION" if lag else "BROKEN", f"pid={lag or 'DEAD'} (verdict pending; run cex_pm_lag_analyze.py)")

    return R


def main():
    R = check_all()
    if "--json" in sys.argv:
        print(json.dumps([{"item": n, "status": s, "detail": d} for n, s, d in R]))
        return
    broken = [r for r in R if r[1] == "BROKEN"]
    order = {"BROKEN": 0, "WARN": 1, "PROBATION": 2, "PROVEN": 3}
    print("=== PROBATION CHECK — everything built stays here until PROVEN ===")
    for n, s, d in sorted(R, key=lambda x: order.get(x[1], 9)):
        mark = {"PROVEN": "✅", "PROBATION": "🟡", "WARN": "⚠️", "BROKEN": "🔴"}.get(s, "?")
        print(f"  {mark} {s:9} {n:24} {d}")
    print(f"\n{len(broken)} BROKEN / {len(R)} tracked" + ("  <-- PAGE" if broken else "  — all in place"))


if __name__ == "__main__":
    main()
