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
    lag = _pgrep("cex_pm_lag_shadow.py")
    add("cex_pm_lag_detector", "PROBATION" if lag else "BROKEN", f"pid={lag or 'DEAD'} (verdict pending; run cex_pm_lag_analyze.py)")

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
