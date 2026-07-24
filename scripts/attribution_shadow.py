#!/usr/bin/env python3
"""attribution_shadow.py — OBSERVE-ONLY counterfactual layer attribution.

Codex's "missing piece": for every FILLED trade (good-config, realized outcome — NO
ghost), log what each admission layer WOULD have decided and whether that decision
would have helped, so each layer's causal effect on PnL is measurable independently
before anything gates a live trade.

Three layers (the proposed dynamic-admission stack):
  base_min_edge  — the coarse candidate-quality floor (from config, per lane/side)
  tape           — own-TF MACD contradicts the side in chop (mirrors tape_arbitration)
  breaker        — lane-direction in a post-loss-cluster cooldown (k consec stops)

Per trade we record mutually-EXCLUSIVE attribution (passed_all / breaker_only /
tape_only / both) and, for each would-block, whether it blocked a WINNER (false_cut,
bad) or a LOSER (save, good). Plus per-lane/side volume + block-rate accounting
(anti-starvation) and per-(lane,side,hour) priors.

SHADOW GUARANTEE: separate process. Reads entries.jsonl, appends one jsonl. Never
imports the bot, never writes config/_runtime_feedback/caps, cannot block a trade.
realized = EXIT-sum (pnl over event==EXIT). Good-config sessions only.

CAVEATS (honest): (a) tape here uses a PROXY for chop — the live tape_arbitration
gate reads an efficiency-ratio (er) from asset_regime that is NOT in the trade log;
we approximate chop via multi-TF MACD disagreement + low convergence_score, and flag
tape rows `tape_proxy=True`. (b) attribution is over FILLED trades only (what got in);
rejected-candidate counterfactuals need ghost settlement, which is untrusted here.
"""
import json, os, sys
import datetime as dt
from collections import defaultdict
from statistics import mean

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESS_ROOT = os.path.join(REPO, "data", "paper_trades")
OUT = os.path.join(REPO, "data", "calibration", "attribution_shadow.jsonl")
CFG = os.path.join(REPO, "config", "settings.yaml")

GOOD_CONFIG_SINCE = "2026-07-21"
MIN_SESSION_TRADES = 20
# reference breaker config for attribution (the shadow-validated safe one)
BREAKER_K = 3
BREAKER_COOLDOWN_MIN = 45
# tape proxy thresholds
TAPE_CONVERGENCE_CHOP_MAX = 0.40   # convergence_score below this = chop-ish
MACD_HIST_EPS = 0.0005             # |histogram| below this = flat/no-momentum


def parse_ts(s):
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def asset(s):
    return s.replace("_macro", "")


def load_base_min_edge():
    """Return {(strategy, window, side): base_min_edge} from config wso, else None."""
    out = {}
    try:
        import yaml
        c = yaml.safe_load(open(CFG))
        for sid, s in (c.get("strategies") or {}).items():
            wso = ((s.get("entry_policy") or {}).get("window_side_overrides") or {})
            for win, sides in wso.items():
                if not isinstance(sides, dict):
                    continue
                for side, o in sides.items():
                    if isinstance(o, dict) and o.get("min_edge") is not None:
                        out[(sid, win, side)] = float(o["min_edge"])
    except Exception as e:
        sys.stderr.write(f"config load failed: {e}\n")
    return out


def breaker_cooldown_intervals(exits):
    """exits: [(exit_ts, is_stop, pnl)] sorted. Return [(start,end)] cooldown windows
    using k consecutive stop-losses -> cooldown. Mirrors directional_breaker_shadow."""
    consec = 0
    intervals = []
    for ts, is_stop, pnl in exits:
        if is_stop and pnl < 0:
            consec += 1
        else:
            consec = 0
        if consec >= BREAKER_K:
            intervals.append((ts, ts + dt.timedelta(minutes=BREAKER_COOLDOWN_MIN)))
            consec = 0
    return intervals


def in_intervals(ts, intervals):
    return any(a <= ts < b for a, b in intervals)


def tape_would_block(action, window, isnap, convergence):
    """Proxy for tape_arbitration: own-TF MACD momentum contradicts the side AND chop.
    Returns (would_block: bool, tape_proxy: bool)."""
    if not isinstance(isnap, dict):
        return False, True
    tf = window if window in ("5m", "15m") else None
    if tf is None:
        return False, True
    hist = isnap.get(f"alt_{tf}_histogram")
    rising = isnap.get(f"alt_{tf}_histogram_rising")
    above = isnap.get(f"alt_{tf}_above_zero")
    if hist is None:
        return False, True
    # own-TF momentum direction: bullish if rising & (above zero or climbing)
    bull_mom = bool(rising) and (bool(above) or float(hist) > MACD_HIST_EPS)
    bear_mom = (not bool(rising)) and (float(hist) < -MACD_HIST_EPS)
    # contradiction: BUY_YES (long) but bearish momentum; BUY_NO (short) but bullish
    contradicts = (action == "BUY_YES" and bear_mom) or (action == "BUY_NO" and bull_mom)
    # chop proxy: low convergence
    chop = convergence is not None and float(convergence) < TAPE_CONVERGENCE_CHOP_MAX
    return (contradicts and chop), True


def main():
    now = dt.datetime.now(dt.timezone.utc)
    base_edge = load_base_min_edge()
    since = dt.datetime.strptime(GOOD_CONFIG_SINCE, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)

    trade_rows = []       # per-trade attribution records
    lane_exits = defaultdict(list)  # for breaker replay

    for name in sorted(os.listdir(SESS_ROOT)):
        if not name.startswith("test_"):
            continue
        try:
            sdate = dt.datetime.strptime(name.split("_")[1], "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        except (IndexError, ValueError):
            continue
        if sdate < since:
            continue
        path = os.path.join(SESS_ROOT, name, "entries.jsonl")
        if not os.path.isfile(path):
            continue
        entries_by_tid = {}
        exits_by_tid = {}  # aggregate to ONE terminal record per trade (Codex: EXIT-sum per trade_id,
                           # so partial exits can never double-count pnl or the breaker stop-cluster)
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    tid = r.get("trade_id")
                    if r.get("event") == "ENTRY":
                        entries_by_tid[tid] = r
                    elif r.get("event") == "EXIT" and tid is not None:
                        ets = parse_ts(r["timestamp"]) if r.get("timestamp") else None
                        agg = exits_by_tid.get(tid)
                        pnl = float(r.get("pnl", 0) or 0)
                        is_stop = "stop" in (r.get("reason", "") or "")
                        if agg is None:
                            exits_by_tid[tid] = {"pnl": pnl, "ts": ets, "is_stop": is_stop}
                        else:
                            agg["pnl"] += pnl
                            # terminal record: keep the latest exit's ts + its stop-ness
                            if ets is not None and (agg["ts"] is None or ets >= agg["ts"]):
                                agg["ts"] = ets
                                agg["is_stop"] = is_stop
        except (OSError, ValueError):
            continue
        if len(exits_by_tid) < MIN_SESSION_TRADES:
            continue
        for tid, agg in exits_by_tid.items():
            en = entries_by_tid.get(tid)
            if en is None or not en.get("timestamp") or agg["ts"] is None:
                continue
            strat = en.get("strategy", "")
            eex = en.get("extra", {}) or {}
            win = eex.get("window_size", "?")
            side = "up" if en.get("action") == "BUY_YES" else "down"
            lane = f"{asset(strat)}|{win}|{side}"
            trade_rows.append({
                "lane": lane, "strategy": strat, "window": win, "side": side,
                "action": en.get("action"),
                "entry_ts": parse_ts(en["timestamp"]),
                "hour_utc": eex.get("hour_utc"),
                "raw_edge": eex.get("edge") if eex.get("edge") is not None else en.get("edge"),
                "convergence": eex.get("convergence_score"),
                "isnap": eex.get("indicator_snapshot"),
                "pnl": agg["pnl"],          # EXIT-sum for this trade
                "is_stop": agg["is_stop"],  # terminal exit's stop-ness
            })
            lane_exits[lane].append((agg["ts"], agg["is_stop"], agg["pnl"]))

    if not trade_rows:
        sys.stderr.write("attribution_shadow: no good-config trades\n")
        return 0

    # breaker cooldown intervals per lane
    cooldowns = {lane: breaker_cooldown_intervals(sorted(ex)) for lane, ex in lane_exits.items()}

    # per-trade attribution
    out_records = []
    for t in trade_rows:
        base = base_edge.get((t["strategy"], t["window"], t["side"]))
        edge_pass = (base is None) or (t["raw_edge"] is not None and float(t["raw_edge"]) >= base)
        wb_breaker = in_intervals(t["entry_ts"], cooldowns.get(t["lane"], []))
        wb_tape, tape_proxy = tape_would_block(t["action"], t["window"], t["isnap"], t["convergence"])
        # mutually-exclusive label among the two dynamic layers (base already passed=filled)
        if wb_breaker and wb_tape:
            label = "both"
        elif wb_breaker:
            label = "breaker_only"
        elif wb_tape:
            label = "tape_only"
        else:
            label = "passed_all"
        is_win = t["pnl"] > 0
        out_records.append({
            "ts_utc": now.isoformat(), "lane": t["lane"], "side": t["side"],
            "hour_utc": t["hour_utc"], "entry_ts": t["entry_ts"].isoformat(),
            "raw_edge": t["raw_edge"], "base_min_edge": base, "edge_pass": edge_pass,
            "would_block_breaker": wb_breaker, "would_block_tape": wb_tape, "tape_proxy": tape_proxy,
            "attribution": label, "pnl": round(t["pnl"], 2), "is_win": is_win,
            "mode": "shadow",
        })

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        for r in out_records:
            f.write(json.dumps(r) + "\n")

    # ---- rollups (Codex asks: winners-blocked vs losers-blocked, block-rate, priors) ----
    def acct(recs, layer_key):
        blocked = [r for r in recs if r[layer_key]]
        save = -sum(r["pnl"] for r in blocked if r["pnl"] < 0)   # losers avoided (+good)
        fcut = sum(r["pnl"] for r in blocked if r["pnl"] > 0)    # winners missed (+bad)
        return len(blocked), round(save, 2), round(fcut, 2), round(save - fcut, 2)

    print(f"attribution_shadow {now.isoformat()} | trades={len(out_records)}")
    n = len(out_records)
    for layer, key in (("breaker", "would_block_breaker"), ("tape", "would_block_tape")):
        bn, save, fcut, net = acct(out_records, key)
        print(f"  {layer:8}: would-block {bn}/{n} ({100*bn/n:.0f}%)  save +{save} / false_cut -{fcut} -> net {net:+.2f}")
    # per-lane net where breaker would net-help most / hurt most
    bylane = defaultdict(list)
    for r in out_records:
        bylane[r["lane"]].append(r)
    print("  per-lane BREAKER net (save-falsecut), lanes with any block:")
    lane_net = []
    for lane, rs in bylane.items():
        bn, save, fcut, net = acct(rs, "would_block_breaker")
        if bn:
            lane_net.append((net, lane, bn, len(rs)))
    for net, lane, bn, tot in sorted(lane_net, reverse=True):
        starve = " <STARVE-RISK>" if bn / tot > 0.5 else ""
        print(f"    {lane:16} net{net:+7.2f}  blocked {bn}/{tot}{starve}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
