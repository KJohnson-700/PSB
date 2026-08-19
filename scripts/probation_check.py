#!/usr/bin/env python3
"""PROBATION CHECK — every change/build stays on this list until PROVEN.

The operator's standing rule (2026-08-05): nothing built gets to be forgotten. Run this EVERY monitor
tick. For each tracked item it verifies the thing is still in place / still alive / hasn't regressed,
and prints PROVEN / PROBATION / BROKEN. BROKEN = a config was reverted, a daemon died, or an error
recurred — page it. PROBATION = live/running but not yet proven by realized data (the metric check,
run separately, decides promotion). Add a row here the moment you ship anything.

Usage: .venv/bin/python scripts/probation_check.py   (add --json for machine output)
"""
import glob
import json
import os
import re
import datetime
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
    # 2026-08-18 RETIRED (loop de-junk list): S2 cex_pm_lag was REFUTED/CLOSED 08-15
    # (all EV cells negative after 2c+fee; even with look-ahead favoring it). The old row
    # reported BROKEN forever because the shadow was deliberately stopped — a dead gauge.
    add("cex_pm_lag_detector", "SUPERSEDED", "S2 refuted+closed 08-15; shadow retired by design")

    # --- 2026-08-16 the two wrong-side-prob fixes, and the eth lanes they put on notice ---
    # Both are the SAME defect class: under side_policy "favorite" the market picks the side, so
    # estimated_prob describes the OTHER side. BTC fed it to true-Kelly (f* <= 0 => every
    # candidate died on kelly_nonpositive, 35.4% of skips, ZERO entries); eth fed it to an
    # `edge <= 0` guard sitting 659 lines UPSTREAM of the flat-edge floor written to prevent
    # exactly that (nonpositive_edge, 9.1%). If either line vanishes the lane silently starves
    # again with no error — hence a row here rather than trust.
    try:
        _btc_src = open(os.path.join(_REPO, "src/strategies/bitcoin.py")).read()
        _eth_src = open(os.path.join(_REPO, "src/strategies/eth_macro.py")).read()
    except Exception:
        _btc_src = _eth_src = ""
    _btc_fix = "float(entry_price) + _side_policy_flat_edge" in _btc_src
    add("btc_kelly_sidepolicy_fix", "PROBATION" if _btc_fix else "BROKEN",
        "win_prob reconstructed from price+flat_edge" if _btc_fix
        else "REVERTED — BTC true-Kelly back on the wrong-side prob, expect 0 entries")
    _eth_fix = _eth_src.count("edge = _side_policy_flat_edge") >= 2
    add("eth_nonposedge_order_fix", "PROBATION" if _eth_fix else "BROKEN",
        "survival floor precedes the nonpositive_edge guard" if _eth_fix
        else "REVERTED — eth edge guard back upstream of its own floor")

    # Both BTC tape guards must keep the CORRECT polarity (delta <= -escape = winning = admit).
    # The 08-12 fix was applied to one guard and missed the other for four days; the inverted
    # form blocks winners and admits losers while raising no error and changing no count.
    _g1 = "_adm_down > -_escape" in _btc_src
    _g2 = "_adm_c > -_c_escape" in _btc_src
    _old = "_adm_down < _escape" in _btc_src or "_adm_c < _c_escape" in _btc_src
    add("btc_tape_guard_polarity", "PROBATION" if (_g1 and _g2 and not _old) else "BROKEN",
        "both guards escape on delta<=-escape (winning)" if (_g1 and _g2 and not _old)
        else f"POLARITY INVERTED — guard1_ok={_g1} guard2_ok={_g2} old_form_present={_old}")

    # GREEN-GATE semantic fix: resolutions must feed the tape adapter a pnl-sign mfe, else
    # green_factor collapses to 0 and a LOSING lane can never be tightened. Restart-class
    # (main.py is NOT in _HOT_RELOAD_CODE_MODULES). BROKEN = reverted -> losers go unshielded again.
    try:
        _mn = open(os.path.join(_REPO, "src/main.py")).read()
    except Exception:
        _mn = ""
    _gg = "_is_resolution = _xr.endswith(\"updown_expired\")" in _mn
    add("green_gate_resolution_mfe", "PROBATION" if _gg else "BROKEN",
        "resolutions feed pnl-sign mfe; catastrophic stops keep real mfe" if _gg
        else "REVERTED — losing lanes cannot be tightened (green_factor pinned to 0)")

    # OPERATOR RULE 2026-08-16: "if that continues and you dont see a fix then we cut."
    # The watchlist owns the thresholds; this row just makes sure it is READ every tick.
    try:
        sys.path.insert(0, os.path.join(_REPO, "scripts"))
        import lane_cut_watchlist as _lcw
        _anchor, _lanes = _lcw.evaluate()
        _bad = [x for x in _lanes if x["state"] in ("CUT NOW", "DEGRADED")]
        _acc = [x for x in _lanes if x["state"] == "ACCRUING"]
        if not _anchor:
            add("eth_cut_watchlist", "WARN",
                "ANCHOR NOT SET — run lane_cut_watchlist.py --set-anchor (verdict would read pre-fix trades)")
        elif _bad:
            add("eth_cut_watchlist", "BROKEN",
                "CUT CONDITION MET: " + "; ".join(f"{x['lane']} ({x['note']})" for x in _bad))
        else:
            add("eth_cut_watchlist", "PROBATION",
                f"{len(_lanes)} lanes watched, {len(_acc)} accruing, 0 at cut — "
                f"detail: scripts/lane_cut_watchlist.py")
    except Exception as e:  # never let the watchlist break the health check
        add("eth_cut_watchlist", "WARN", f"watchlist unreadable: {type(e).__name__}")

    # 2026-08-17 TOOLING DAEMON. Two pipelines were dead for DAYS and neither said so:
    # ghost rotation (launchd agent, TCC-blocked on ~/Documents -> exit 1 every run while
    # the reject log grew to 1.3GB) and the stopped-trade settler (hand-started --loop,
    # died with the shell). Liveness is NOT the test here — per the standing rule, check
    # OUTPUT: the settler must be WRITING, and the reject log must be BOUNDED. A daemon
    # that is up but producing nothing is the exact failure this row exists to catch.
    _pidf = os.path.join(_REPO, "data/calibration/psb_tooling_daemon.pid")
    _alive = False
    try:
        with open(_pidf) as fh:
            os.kill(int(fh.read().strip()), 0)
        _alive = True
    except Exception:
        _alive = False

    # ⛔ DO NOT use the output file's mtime here. The settler is IDEMPOTENT — it skips
    # already-settled trade_ids — so when there is nothing new to settle it runs fine and
    # writes NOTHING, and mtime does not advance. That produced a false REGRESSION at
    # "settler_write_age=144m" while the daemon log showed it running every 30 min on
    # schedule (and zero new stops to settle is GOOD news, not a fault). The real signal is
    # whether the settler RAN, which the daemon log records.
    _settled_age_min = None
    try:
        _dlog = os.path.join(_REPO, "data/calibration/psb_tooling_daemon.log")
        _last_run = None
        with open(_dlog, errors="ignore") as fh:
            for _ln in fh:
                if "SETTLER:" not in _ln:
                    continue
                try:
                    _last_run = datetime.datetime.strptime(
                        _ln.split(" ", 1)[0], "%Y-%m-%dT%H:%M:%S%z"
                    )
                except Exception:
                    continue
        if _last_run is not None:
            _settled_age_min = (
                datetime.datetime.now(datetime.timezone.utc) - _last_run
            ).total_seconds() / 60.0
    except Exception:
        pass

    _reject_mb = None
    try:
        _reject_mb = os.path.getsize(
            os.path.join(_REPO, "data/calibration/rejected_candidates.jsonl")
        ) / 1024 / 1024
    except Exception:
        pass

    # Settler runs every 30 min, so >90 min of no write means it is not producing.
    # Rotate threshold is 200MB and fires daily; >1500MB means rotation is not landing.
    _stale_settler = _settled_age_min is not None and _settled_age_min > 90
    _unbounded = _reject_mb is not None and _reject_mb > 1500
    _detail = (
        f"daemon={'up' if _alive else 'DOWN'} "
        f"settler_last_run={'?' if _settled_age_min is None else f'{_settled_age_min:.0f}m ago'} "
        f"reject_log={'?' if _reject_mb is None else f'{_reject_mb:.0f}MB'}"
    )
    if not _alive:
        add("tooling_daemon", "BROKEN",
            f"{_detail} — relaunch: nohup scripts/psb_tooling_daemon.sh > /dev/null 2>&1 < /dev/null & disown")
    elif _stale_settler or _unbounded:
        add("tooling_daemon", "BROKEN",
            f"{_detail} — daemon is UP but not PRODUCING (check OUTPUT, not liveness)")
    else:
        add("tooling_daemon", "PROBATION",
            f"{_detail} — settler running on cadence, reject log bounded")

    # 2026-08-17 RELEASE GUARDS. cut_reopen_tripwire + floor_release_monitor were
    # TCC-disabled 08-14, and when I checked before re-arming them BOTH watched gates
    # that no longer exist (min_edge 0.30 cut: gone, live floors 0.05-0.09; pocket_off
    # reasons: 0 of 1,823 rows). Replaced by blocked_band_guard.py against the live
    # reasons. This row exists because a release guard that reads zero is INDISTINGUISHABLE
    # from a gate that is behaving — the thing that let the originals rot for weeks.
    _g_reasons_live = 0
    try:
        import collections as _c
        _seen = _c.Counter()
        with open(os.path.join(_REPO, "data/calibration/rejected_candidates.jsonl"),
                  errors="ignore") as fh:
            for _ln in fh:
                try:
                    _r = str((json.loads(_ln) or {}).get("reason") or "")
                except Exception:
                    continue
                if _r == "rsi_hard_blocked" or _r.endswith("_disabled_lane"):
                    _seen[_r] += 1
        _g_reasons_live = sum(_seen.values())
    except Exception:
        _g_reasons_live = -1

    _g_out = os.path.join(_REPO, "data/calibration/floor_release_shadow.jsonl")
    _g_age_min = None
    try:
        _g_age_min = (time.time() - os.stat(_g_out).st_mtime) / 60.0
    except Exception:
        pass

    if not os.path.isfile(os.path.join(_REPO, "scripts/blocked_band_guard.py")):
        add("release_guards", "BROKEN", "blocked_band_guard.py MISSING — no reopen/release watch")
    elif _g_reasons_live == 0:
        # the watched reasons vanished from the log = the gates moved again = guard is blind
        add("release_guards", "BROKEN",
            "0 rows for rsi_hard_blocked / *_disabled_lane — the watched gate reasons MOVED again; "
            "re-derive targets before trusting any 'gate_holding' verdict")
    else:
        add("release_guards", "PROBATION",
            f"watching {_g_reasons_live} live blocked rows; last write "
            f"{'never' if _g_age_min is None else f'{_g_age_min:.0f}m ago'} — FLAG-only, never writes config")

    # 2026-08-18 ASSET STARVATION (operator: "btc hasnt traded all night how is this not a
    # red flag"). CLAUDE.md defines a 24h+ zero-trade window on any asset as a red flag —
    # yet nothing paged when BTC went ~36h dark (last entry 08-17 10:03 UTC; the 08-18
    # systems check verified process/config/feeds and never counted per-asset entries).
    # This row closes that hole: any ENABLED strategy with zero ENTRY rows across the
    # trailing 24h of session journals goes BROKEN and pages. An asset whose every lane is
    # deliberately paused in lane_management.states is skipped (a chosen pause is not
    # starvation — see feedback_baseline_restore_never_revert_deliberate_lane_disables).
    try:
        _now = datetime.datetime.now(datetime.timezone.utc)
        _lane_states = ((c.get("lane_management") or {}).get("states") or {})
        _last_entry = {}
        _sess_paths = sorted(glob.glob(os.path.join(_REPO, "data/paper_trades/test_*/entries.jsonl")))[-8:]
        for _sp in _sess_paths:
            with open(_sp, errors="ignore") as fh:
                for _ln in fh:
                    if '"ENTRY"' not in _ln:
                        continue
                    try:
                        _r = json.loads(_ln)
                    except Exception:
                        continue
                    if _r.get("event") != "ENTRY":
                        continue
                    _st = _r.get("strategy")
                    _ts = _r.get("timestamp") or ""
                    if _st and _ts > _last_entry.get(_st, ""):
                        _last_entry[_st] = _ts
        _starved = []
        for _name, _scfg in (strat or {}).items():
            if not isinstance(_scfg, dict) or _scfg.get("enabled") is False:
                continue
            # Skip only when the asset is FULLY paused — all 6 window|side lanes explicitly
            # paused. A partial pause is NOT exempt: that is exactly how BTC starved for 36h
            # unnoticed (bitcoin|5m|up paused 08-12 = the only lane that ever produced volume,
            # while 15m/1h stayed "enabled" behind unreachable min-edge bars).
            _all_lanes = [f"{_name}|{_w}|{_s}" for _w in ("5m", "15m", "1h") for _s in ("up", "down")]
            if all(str(_lane_states.get(_k, "")).lower() == "paused" for _k in _all_lanes):
                continue  # whole asset deliberately paused
            _ts = _last_entry.get(_name)
            if not _ts:
                _starved.append(f"{_name}=NEVER(in last {len(_sess_paths)} sessions)")
                continue
            try:
                _age_h = (_now - datetime.datetime.fromisoformat(_ts)).total_seconds() / 3600.0
            except Exception:
                continue
            if _age_h >= 24:
                _starved.append(f"{_name}={_age_h:.0f}h")
        if _starved:
            add("asset_starvation_24h", "BROKEN",
                "ZERO entries >24h (CLAUDE.md red flag, NOT 'working as designed'): "
                + ", ".join(_starved))
        else:
            add("asset_starvation_24h", "PROVEN",
                f"all enabled assets entered within 24h (checked {len(_sess_paths)} sessions)")
    except Exception as _e:
        add("asset_starvation_24h", "WARN", f"unreadable: {type(_e).__name__}")

    # 2026-08-18 EST-CAL SIZING CONSUMER (#27). Phase A's state refit on cadence but had
    # ZERO consumers in src/ — "sizing-only" was a state file nothing read. The consumer
    # (src/analysis/est_cal.py, wired at all three true-Kelly sites) self-arms on the
    # pre-registered walkforward gate (>=150 OOS, cal beats raw AND market). BROKEN =
    # the wiring vanished; PROBATION = wired (gated or armed, both are correct states).
    try:
        _ec_src = open(os.path.join(_REPO, "src/analysis/est_cal.py")).read()
        _ec_wired = all("from src.analysis.est_cal import sized_win_prob" in
                        open(os.path.join(_REPO, f"src/strategies/{f}.py")).read()
                        for f in ("sol_macro", "eth_macro", "bitcoin"))
        sys.path.insert(0, _REPO)
        from src.analysis.est_cal import gate_status as _ec_gate
        _ec_g = _ec_gate()
        add("est_cal_sizing_consumer",
            "PROBATION" if _ec_wired else "BROKEN",
            (f"wired 3/3 sites; {'ARMED' if _ec_g.get('armed') else 'gated'} — {_ec_g.get('why')}")
            if _ec_wired else "consumer import MISSING from a strategy — sizing hook reverted")
    except Exception as _e:
        add("est_cal_sizing_consumer", "WARN", f"unreadable: {type(_e).__name__}")

    # 2026-08-18 ENTRY BOOK OBSERVE (#29). paper_entry_fresh_fill shipped FALSE in its own
    # ship commit -> entry_paper_fill_quality null on 100% of entries. Observe-only mode
    # records the book with zero fill change. Verify from OUTPUT once restarted: recent
    # ENTRY rows must carry a populated paper_fill_quality. Until the restart the row
    # reads PROBATION (staged, restart-class).
    try:
        _obs_flag = bool((trad.get("paper_entry_book_observe", False)))
        _obs_pop = 0
        _obs_tot = 0
        for _sp in sorted(glob.glob(os.path.join(_REPO, "data/paper_trades/test_*/entries.jsonl")))[-1:]:
            for _ln in open(_sp, errors="ignore"):
                if '"ENTRY"' not in _ln:
                    continue
                try:
                    _r = json.loads(_ln)
                except Exception:
                    continue
                if _r.get("event") != "ENTRY":
                    continue
                _obs_tot += 1
                if ((_r.get("extra") or {}).get("paper_fill_quality")):
                    _obs_pop += 1
        if not _obs_flag:
            add("entry_book_observe", "BROKEN", "paper_entry_book_observe flipped OFF")
        elif _obs_pop > 0:
            add("entry_book_observe", "PROVEN",
                f"{_obs_pop}/{_obs_tot} current-session entries carry book data")
        else:
            add("entry_book_observe", "PROBATION",
                f"flag ON, staged (restart-class); current session {_obs_pop}/{_obs_tot} populated")
    except Exception as _e:
        add("entry_book_observe", "WARN", f"unreadable: {type(_e).__name__}")

    # 2026-08-18 FAVORITE LANE UN-STRANGLED. respect_ai_direction required the BENCHED
    # direction engine to name a side => 100% of favorite candidates sat out since
    # 08-14T01:45 and the operator's 08-13 xrp|1h+hype|15m trial never ran. false = the
    # configuration the 95-100% WR record was built under. Verify from OUTPUT after
    # restart: SIT-OUT log lines stop / favorite entries reappear on the two lanes.
    try:
        _fl = (c.get("favorite_lane") or {})
        _fl_states = ((c.get("lane_management") or {}).get("states") or {})
        # operator option 2 (08-18): favorites bypass the band pauses on exactly the two
        # trial lanes via more-specific state keys (favorite lane_ids are regime-pinned).
        _fl_bypass = (str(_fl_states.get("xrp_macro|1h|up|favorite", "")).lower() == "paper"
                      and str(_fl_states.get("hype_macro|15m|up|favorite", "")).lower() == "paper")
        _fl_ok = (_fl.get("respect_ai_direction") is False
                  and _fl.get("allow_lanes") == ["xrp_macro|1h", "hype_macro|15m"]
                  and _fl_bypass)
        add("favorite_lane_unstrangled",
            "PROBATION" if _fl_ok else "BROKEN",
            f"respect_ai_direction={_fl.get('respect_ai_direction')} allow_lanes={_fl.get('allow_lanes')} "
            f"pause_bypass_keys={'ok' if _fl_bypass else 'MISSING'}"
            + ("" if _fl_ok else " — EXPECTED false + 2-lane allowlist + |favorite paper keys"))
    except Exception as _e:
        add("favorite_lane_unstrangled", "WARN", f"unreadable: {type(_e).__name__}")

    # 2026-08-18 BLANKET SHRINK KILLED (49563e7). entry_admission_calibration_shrink 0.28
    # was the mid-June regression: shipped per-lane 06-23, flattened to strategy-level by
    # the 07-13/07-21 deploys, deleting 72% of model deviation on 5 assets (June median
    # |est-price| 0.08-0.09 -> 0.005-0.015). BROKEN = a 0.28 strategy-level key reappeared
    # (the backup-restore trap). Revert bar + procedure: vault note
    # 2026-08-18-SHIP-blanket-shrink-killed-revert-bar.md — revert is PER-LANE scope only,
    # never the blanket.
    try:
        _shrunk = [s for s in ("bitcoin", "sol_macro", "eth_macro", "hype_macro",
                               "xrp_macro", "doge_macro", "bnb_macro")
                   if float((strat.get(s) or {}).get("entry_admission_calibration_shrink", 1.0) or 1.0) < 1.0]
        add("blanket_shrink_killed",
            "PROBATION" if not _shrunk else "BROKEN",
            "all strategy-level shrink keys at 1.0 — est-cal is the only calibration layer"
            if not _shrunk else
            f"STRATEGY-LEVEL SHRINK REAPPEARED on {_shrunk} — the June regression is back; "
            "see vault 2026-08-18-SHIP-blanket-shrink-killed-revert-bar.md")
    except Exception as _e:
        add("blanket_shrink_killed", "WARN", f"unreadable: {type(_e).__name__}")

    # 2026-08-19 EXPIRY SETTLE DEFER + FRESH-FILL STRICT + RTDS ORACLE (operator GO bundle).
    # All three verify from OUTPUT: (1) no NEW updown_expired rows with a non-binary
    # exit_price (the 08-18 fake-settle signature — 10 in one night); (2) fresh-fill flag ON;
    # (3) the RTDS snapshot file advancing (a dead ws = stale file, not an error anywhere).
    try:
        _fake_new = 0
        _seen_any = False
        with open(os.path.join(_REPO, "data/calibration/trades.jsonl"), errors="ignore") as fh:
            for _ln in fh:
                if '"updown_expired"' not in _ln:
                    continue
                try:
                    _r = json.loads(_ln)
                except Exception:
                    continue
                if str(_r.get("opened_at") or "") < "2026-08-19T07:30":
                    continue
                if _r.get("exit_reason") != "updown_expired":
                    continue
                _seen_any = True
                _xp = _r.get("exit_price")
                if _xp is not None and 0.02 < float(_xp) < 0.98:
                    _fake_new += 1
        if _fake_new:
            add("expiry_settle_defer", "BROKEN",
                f"{_fake_new} NEW fake settle(s) post-ship (updown_expired at non-binary exit_price) "
                "— the defer branch reverted or the grace is too short")
        else:
            add("expiry_settle_defer", "PROBATION",
                f"0 fake settles post-ship ({'binary settles observed' if _seen_any else 'no expiries graded yet'}); "
                "grace=exit_rules.updown_expiry_grace_mins")
    except Exception as _e:
        add("expiry_settle_defer", "WARN", f"unreadable: {type(_e).__name__}")

    add("paper_fresh_fill_strict",
        "PROBATION" if bool(trad.get("paper_entry_fresh_fill")) else "BROKEN",
        f"paper_entry_fresh_fill={trad.get('paper_entry_fresh_fill')} "
        + ("(book-walk fills + no-fill on uncrossable books)" if trad.get("paper_entry_fresh_fill")
           else "— flipped back OFF (shipped-dark trap, e921345 class)"))

    try:
        _rt = os.path.join(_REPO, "data/calibration/rtds_snapshots.jsonl")
        _rt_age = (time.time() - os.stat(_rt).st_mtime) / 60.0 if os.path.isfile(_rt) else None
        if not bool((c.get("rtds") or {}).get("enabled")):
            add("rtds_oracle", "BROKEN", "rtds.enabled flipped OFF")
        elif _rt_age is None:
            add("rtds_oracle", "PROBATION", "staged — no snapshot file yet (restart-class; verify after boot)")
        elif _rt_age > 10:
            add("rtds_oracle", "BROKEN",
                f"snapshot file STALE {_rt_age:.0f}m — ws down or task dead (check OUTPUT not flag)")
        else:
            add("rtds_oracle", "PROBATION", f"snapshots advancing (last {_rt_age:.1f}m ago)")
    except Exception as _e:
        add("rtds_oracle", "WARN", f"unreadable: {type(_e).__name__}")

    # 2026-08-17 CONFIG AUDIT (architecture item 4). Read-only. Catches the classes that
    # have each already cost a wrong conclusion: same-mapping duplicate keys (bnb had one
    # at HEAD), keys nothing reads, restart-class keys edited under a running bot, paused
    # lanes still taking entries, min-edge/price-band contradictions, and comments that
    # contradict their own value.
    # 2026-08-17 SESSION LEDGER (architecture item 2). The cross-session ledger was already
    # complete for CLOSED trades (EXIT events match trades.jsonl 1:1 every session), so the
    # real damage is ORPHANED OPEN POSITIONS: a restart abandons whatever is open, those
    # never close, never reach the ledger, and their P&L is missing from every review.
    # Measured 197 across 88 folders. They ARE recoverable — up/down markets resolve — so
    # this row tracks the UNSETTLED backlog. A growing backlog means restarts are eating
    # money silently, which is exactly what "a restart cannot hide part of the run" means.
    try:
        _lids = set()
        with open(os.path.join(CAL, "trades.jsonl"), errors="ignore") as fh:
            for _ln in fh:
                try:
                    _lids.add(str(json.loads(_ln).get("trade_id")))
                except Exception:
                    continue
        _settled_orph = set()
        _op = os.path.join(CAL, "orphaned_positions_settled.jsonl")
        if os.path.isfile(_op):
            with open(_op, errors="ignore") as fh:
                for _ln in fh:
                    try:
                        _settled_orph.add(str(json.loads(_ln).get("trade_id")))
                    except Exception:
                        continue
        _paper = os.path.join(_REPO, "data/paper_trades")
        _unsettled = 0
        for _s in os.listdir(_paper):
            _pp = os.path.join(_paper, _s, "positions.json")
            if not os.path.isfile(_pp):
                continue
            try:
                _j = json.load(open(_pp, encoding="utf-8"))
            except Exception:
                continue
            _items = _j if isinstance(_j, list) else (
                list(_j.values()) if isinstance(_j, dict) else [])
            for _p in _items:
                if not isinstance(_p, dict):
                    continue
                _tid = str(_p.get("trade_id") or "")
                if _tid and _tid not in _lids and _tid not in _settled_orph:
                    _unsettled += 1
        if _unsettled > 25:
            add("session_ledger", "BROKEN",
                f"{_unsettled} orphaned positions UNSETTLED — restart-abandoned P&L missing "
                f"from every review; run scripts/session_ledger.py --settle-orphans")
        else:
            add("session_ledger", "PROBATION",
                f"{_unsettled} unsettled orphan(s), {len(_settled_orph)} recovered — "
                f"detail: scripts/session_ledger.py")
    except Exception as e:
        add("session_ledger", "WARN", f"ledger check failed: {type(e).__name__}")

    # 2026-08-17 ENTRY/EXIT SPLIT (architecture item 3). The two buckets are only comparable
    # when both cover the SAME trades, so the health metric is BUCKET-A COVERAGE: what share
    # of exit-layer trades have a settled held-to-resolution counterfactual. Low coverage does
    # not read as "no exit leak", it reads as "we cannot tell" — and the first version of the
    # tool inflated the delta by +$73 precisely because it compared unequal trade sets.
    try:
        _res_reasons = {"updown_expired", "RESOLVED:YES (real)", "RESOLVED:NO (real)"}
        _held_ids = set()
        for _lp in ("stopped_trades_settled.jsonl", "orphaned_positions_settled.jsonl",
                    "exit_layer_settled.jsonl"):
            _p = os.path.join(CAL, _lp)
            if not os.path.isfile(_p):
                continue
            with open(_p, errors="ignore") as fh:
                for _ln in fh:
                    try:
                        _r = json.loads(_ln)
                    except Exception:
                        continue
                    if _r.get("held_pnl_net") is not None or _r.get("settled_pnl_net") is not None:
                        _held_ids.add(str(_r.get("trade_id")))
        _early = _covered = 0
        with open(os.path.join(CAL, "trades.jsonl"), errors="ignore") as fh:
            for _ln in fh:
                try:
                    _t = json.loads(_ln)
                except Exception:
                    continue
                if _t.get("pnl") is None or str(_t.get("opened_at") or "") < "2026-08-13":
                    continue
                if str(_t.get("exit_reason")) in _res_reasons:
                    continue
                _early += 1
                if str(_t.get("trade_id")) in _held_ids:
                    _covered += 1
        _pct = (_covered / _early * 100) if _early else 100.0
        if _early and _pct < 50.0:
            add("entry_exit_split", "BROKEN",
                f"bucket-A coverage {_covered}/{_early} ({_pct:.0f}%) of post-08-13 exit-layer "
                f"trades — the split cannot separate entry from exit; "
                f"run scripts/entry_exit_split.py --settle")
        else:
            add("entry_exit_split", "PROBATION",
                f"bucket-A coverage {_covered}/{_early} ({_pct:.0f}%) post-08-13 — "
                f"detail: scripts/entry_exit_split.py")
    except Exception as e:
        add("entry_exit_split", "WARN", f"split check failed: {type(e).__name__}")

    # 2026-08-17 EST_PROB CALIBRATION (Step 2 Phase A, operator GO). Graded on OUTPUT:
    # the state file must be fresh (tooling daemon refits every settle tick, so >3h stale
    # means the pipeline died — the exact launchd-agent failure mode this checklist exists
    # to catch) and its own recorded coverage must hold. Consumer is SIZING ONLY; the
    # graduation gate (Phase C) reads state["walkforward"], never the in-sample Brier.
    try:
        _ecal = os.path.join(CAL, "est_prob_calibration.json")
        if not os.path.isfile(_ecal):
            add("est_calibration", "BROKEN",
                "est_prob_calibration.json MISSING — run scripts/est_calibration_report.py --write-state")
        else:
            _age_h = (time.time() - os.path.getmtime(_ecal)) / 3600.0
            with open(_ecal) as _fh:
                _st = json.load(_fh)
            _cov = float(_st.get("coverage_pct") or 0.0)
            _wf = _st.get("walkforward") or {}
            _wf_txt = (f"walkforward n={_wf.get('n')} brier cal {_wf.get('calibrated')} "
                       f"vs mkt {_wf.get('market')}" if _wf else "walkforward: pending")
            if _age_h > 3.0:
                add("est_calibration", "BROKEN",
                    f"state {_age_h:.1f}h stale (daemon refits every settle tick) — "
                    f"check psb_tooling_daemon EST-CAL lines")
            elif _cov < 50.0:
                add("est_calibration", "BROKEN",
                    f"coverage {_cov:.0f}% <50% — fits untrustworthy; settle is behind")
            else:
                add("est_calibration", "PROBATION",
                    f"state {_age_h:.1f}h old, coverage {_cov:.0f}%, "
                    f"graded_n={_st.get('graded_n')}, {_wf_txt}")
    except Exception as e:
        add("est_calibration", "WARN", f"est-cal check failed: {type(e).__name__}")

    # 2026-08-17 FINGERPRINT CONTRACT (operator GO). The 102-session forensic showed every
    # big winner satisfied six geometry invariants and every Aug losing era violated at
    # least one — each dismantling step was locally justified and invisible until the
    # bleed. This row pages the moment the rolling exit-shape breaks the winning
    # fingerprint, which is the level at which "behavior shifts after restart" actually
    # matters. VIOLATION => BROKEN (rc=4 from the script). ACCRUING (< 25 closes since
    # the geometry-restore anchor) is not a violation.
    _fp = os.path.join(_REPO, "scripts/fingerprint_contract.py")
    if not os.path.isfile(_fp):
        add("fingerprint_contract", "BROKEN", "scripts/fingerprint_contract.py MISSING")
    else:
        try:
            _p = subprocess.run(
                [os.path.join(_REPO, ".venv/bin/python"), _fp, "--json"],
                capture_output=True, text=True, timeout=120,
            )
            _j = json.loads((_p.stdout or "").strip().splitlines()[-1])
            _mtx = (f"n={_j.get('n')} b={_j.get('b')} stop={_j.get('stop_share')} "
                    f"tp={_j.get('tp_share')} res={_j.get('res_share')} "
                    f"lossD={_j.get('loss_depth')} avgE={_j.get('avg_entry')}")
            if _j.get("verdict") == "VIOLATION":
                add("fingerprint_contract", "BROKEN",
                    f"WINNING SHAPE BROKEN: {'; '.join(_j.get('violations', []))} [{_mtx}]")
            elif _j.get("verdict") == "ACCRUING":
                add("fingerprint_contract", "PROBATION",
                    f"accruing {_j.get('n')}/25 closes since restore anchor [{_mtx}]")
            else:
                add("fingerprint_contract", "PROBATION",
                    f"shape OK [{_mtx}]")
        except Exception as e:
            add("fingerprint_contract", "WARN",
                f"contract check failed: {type(e).__name__}")

    # 2026-08-17 EVER-GREEN GIVE-BACK FLOOR (operator GO, Codex GO). Riders (peak MFE>=8%)
    # on 5m/15m went 0/5 to -103% forfeits post-restore; the giveback floor cuts them at
    # peak-40pts instead. BROKEN = a 5m/15m updown_expired FULL-FORFEIT (pnl<=-95% of
    # stake) with peak MFE >= 8% AFTER the code ships (means the floor is not delivering).
    # Until the first evergreen_giveback_stop label appears, the row reads PROBATION.
    _GB_SHIP_TS = "2026-08-18T05:50:00"
    try:
        _gb_bad = 0
        _gb_fired = 0
        for _f_ in sorted(glob.glob(os.path.join(_REPO, "data/paper_trades/test_*/entries.jsonl")))[-2:]:
            with open(_f_, errors="ignore") as _fh_:
                for _ln_ in _fh_:
                    try:
                        _d_ = json.loads(_ln_)
                    except Exception:
                        continue
                    if _d_.get("event") != "EXIT" or str(_d_.get("timestamp", "")) < _GB_SHIP_TS:
                        continue
                    _x_ = _d_.get("extra") or {}
                    if str(_x_.get("hold_policy_applied") or "") == "evergreen_giveback_stop":
                        _gb_fired += 1
                    if "expired" not in (_d_.get("reason") or ""):
                        continue
                    if str(_x_.get("lane_window") or "") not in ("5m", "15m"):
                        continue
                    try:
                        _mfe_ = float(_x_.get("mfe_pct") or 0.0)
                        _nt_ = float(_d_["size"]) * float(_d_["entry_price"])
                        _dep_ = float(_d_["pnl"]) / _nt_ if _nt_ > 0 else 0.0
                    except Exception:
                        continue
                    if _mfe_ >= 0.08 and _dep_ <= -0.95:
                        _gb_bad += 1
        if _gb_bad > 0:
            add("evergreen_giveback", "BROKEN",
                f"{_gb_bad} ever-green 5m/15m rider(s) STILL full-forfeited after ship "
                f"— giveback floor not delivering (restart missing or branch dead)")
        else:
            add("evergreen_giveback", "PROBATION",
                f"no rider forfeits since ship; giveback fired {_gb_fired}x "
                f"(restart-class — inert until next restart)")
    except Exception as e:
        add("evergreen_giveback", "WARN", f"giveback check failed: {type(e).__name__}")

    # 2026-08-18 PHASE 1 HOT SESSION ROLLOVER (operator GO, Codex 6 conditions).
    # data/session_rollover.request drains + rolls the journal without a restart.
    # BROKEN = a "failed" rollover event in the newest bot log. Otherwise PROBATION,
    # reporting the last executed rollover if any. RESTART-class until first loaded.
    try:
        _rl_logs = sorted(glob.glob(os.path.join(_REPO, "data/logs/polybot_*.log")))
        _rl_fail = _rl_exec = None
        if _rl_logs:
            with open(_rl_logs[-1], errors="ignore") as _fh_:
                for _ln_ in _fh_:
                    if '"event": "session_rollover"' not in _ln_:
                        continue
                    if '"status": "failed"' in _ln_:
                        _rl_fail = _ln_.strip()[-220:]
                    elif '"status": "executed"' in _ln_:
                        _rl_exec = _ln_.strip()[-220:]
        if _rl_fail:
            add("session_rollover", "BROKEN", f"rollover FAILED: ...{_rl_fail[-160:]}")
        elif _rl_exec:
            add("session_rollover", "PROBATION", f"last executed: ...{_rl_exec[-160:]}")
        else:
            add("session_rollover", "PROBATION",
                "shipped, unexercised (restart-class — loads at next restart; "
                "trigger: write current session id or 'now' to data/session_rollover.request)")
    except Exception as e:
        add("session_rollover", "WARN", f"rollover check failed: {type(e).__name__}")

    # 2026-08-18 PHASE 2 ARMED STANDBY + PRE-FLIGHT (task #22). live_preflight OPS_JSON
    # events prove live execution WOULD work (read-only). BROKEN = last preflight failed.
    try:
        _pf_logs = sorted(glob.glob(os.path.join(_REPO, "data/logs/polybot_*.log")))
        _pf_last = None
        if _pf_logs:
            with open(_pf_logs[-1], errors="ignore") as _fh_:
                for _ln_ in _fh_:
                    if '"event": "live_preflight"' in _ln_:
                        _pf_last = _ln_.strip()
        if _pf_last is None:
            add("live_standby_preflight", "PROBATION",
                "unexercised (restart-class — arms at next restart; on-demand: "
                "touch data/live_preflight.request)")
        elif '"status": "pass"' in _pf_last:
            add("live_standby_preflight", "PROBATION", f"last preflight PASS: ...{_pf_last[-140:]}")
        else:
            add("live_standby_preflight", "BROKEN", f"last preflight FAILED: ...{_pf_last[-160:]}")
    except Exception as e:
        add("live_standby_preflight", "WARN", f"preflight check failed: {type(e).__name__}")

    _audit = os.path.join(_REPO, "scripts/config_audit.py")
    if not os.path.isfile(_audit):
        add("config_audit", "BROKEN", "scripts/config_audit.py MISSING")
    else:
        try:
            _p = subprocess.run(
                [os.path.join(_REPO, ".venv/bin/python"), _audit, "--quiet"],
                capture_output=True, text=True, timeout=120,
            )
            _txt = _p.stdout or ""
            _f = _txt.count("🔴 FAIL")
            _w = _txt.count("🟡 WARN")
            if _p.returncode == 2:
                add("config_audit", "BROKEN", "settings.yaml DOES NOT PARSE")
            elif _f:
                _first = next((l.strip() for l in _txt.splitlines() if "FAIL" in l), "")
                add("config_audit", "BROKEN",
                    f"{_f} FAIL / {_w} WARN — {_first[:140]}")
            else:
                add("config_audit", "PROBATION",
                    f"0 FAIL / {_w} WARN — detail: scripts/config_audit.py")
        except Exception as e:
            add("config_audit", "WARN", f"audit did not run: {type(e).__name__}")

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
