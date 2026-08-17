#!/usr/bin/env python3
"""blocked_band_guard.py — the two RELEASE guards, rebuilt against the LIVE gates.

WHAT THIS REPLACES AND WHY (2026-08-17)
───────────────────────────────────────
`cut_reopen_tripwire.py` and `floor_release_monitor.py` were disabled 08-14 for TCC.
Before re-arming them I checked whether they still measure anything. They do not —
both watch gates that no longer exist:

  cut_reopen_tripwire   watches the 2026-07-23 cut at min_edge **0.30**.
                        Live floors on those 4 lanes are 0.05-0.09. The 0.30 cut is gone.
                        Reasons `lane_min_edge` / `min_edge`: 0 of 1,823 current rows.
                        Two of its lanes (xrp|1h|up, hype|15m|up) moved to the
                        lane_management pause list — a different mechanism entirely.
                        ⛔ It also had NO time filter, so it era-pooled the reject log —
                        the exact trap that faked a "+3.88 BEAT at +3.2 sigma".

  floor_release_monitor watches `buy_no_5m_pocket_off` / `buy_no_15m_pocket_off` /
                        `eth_buy_no_rsi_floor_off`: 0 of 1,823 current rows.
                        RSI blocking now logs as `rsi_hard_blocked` (239 rows, live).

Re-arming either as-is would produce silent zeros forever — "a daemon that is up but
producing nothing", the failure the probation checklist exists to catch.

Both ALSO joined resolutions by grepping `Market <id> resolved:` out of data/logs/*.log.
That is now 7 lines in a whole day's log across a 3.6GB glob. This uses
`ghost_calibration.fetch_resolution` (GAMMA API) instead — the same real-resolution
source `settle_stopped_trades.py` uses.

THE TWO GUARDS, RETARGETED
──────────────────────────
  cut   — the live lane cuts: reasons matching `*_disabled_lane` (the config
          `disable_buy_yes_15m` / `disable_buy_no_15m` family). Auto-discovered from the
          log, NOT hardcoded, so it cannot rot the way the 0.30 list did.
  rsi   — `rsi_hard_blocked`, the live successor to the pocket/RSI floors.

⛔ DELIBERATELY OUT OF SCOPE — already adjudicated, do not re-probe:
  neutral_bias        778 rows / 43% of all rejections. TOP gate on all 7 assets. The
                      probe was NOT graduated (n=88-107 against a 300 bar, and the beats
                      were arithmetic MIRRORS). Standing rule: do not loosen it.
  bleed_hour_sit_out  408 rows. Adaptive, and already cleared as NOT the starvation cause.
  eth_fade_shadow     a shadow's own log line, not a gate.

HOW IT GRADES — and how it does NOT
───────────────────────────────────
Primary metric is **EV per $1 staked** against the REAL resolution. Not win rate: the
breakeven WR for a contract bought at price p is exactly p, so a flat "WR > 0.52" bar is
meaningless across a price range. Both old guards used 0.52; they only survived it
because they ANDed it with EV>0. Here EV is the test and mean entry price is printed so
the reader can see the breakeven the lane actually faced.

⚠️ This is a would-win counterfactual on BLOCKED candidates: it asks "if this had been
allowed and held to resolution, what would it have returned." Since exits are killed and
updown_expired is ~100% of closes, that counterfactual now matches real behaviour — but
it is still inference. It NEVER writes config, never reopens a lane, never lifts a floor.
It FLAGS for a human. [[feedback_ghost_data_do_not_trust_hard_rule]]

SHADOW GUARANTEE: separate process, reads the reject log, appends one jsonl per guard,
no bot import, no config write, cannot affect a trade.

USAGE
  scripts/blocked_band_guard.py --guard both --once
  scripts/blocked_band_guard.py --guard rsi --hours 24 --limit 400
"""
import argparse
import json
import os
import sys
import time
import datetime as dt
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = os.path.join(REPO, "data", "calibration")
REJECT = os.path.join(CAL, "rejected_candidates.jsonl")
OUT = {
    "cut": os.path.join(CAL, "cut_reopen_shadow.jsonl"),
    "rsi": os.path.join(CAL, "floor_release_shadow.jsonl"),
}

# n before any verdict. Below this the honest answer is "insufficient", not a lean.
MIN_N = 10
# EV per $1 must clear this to be a candidate at all.
FLAG_EV = 0.0
# ...AND it must clear this many sigma. THIS IS THE POINT OF THE WHOLE FILE.
# Binary payoffs have ~1.0 std per trade, so SE ~= 1/sqrt(n): at n=50 one sigma is
# ~0.14 EV/$. Without this bar the first deduped run flagged FIVE lanes, including
# eth|1h|down at EV +0.002 / BEAT +0.1pts — pure noise dressed as a finding, and the
# same class of error as the retracted "+3.88 BEAT at +3.2 sigma" (which was era
# pooling). A guard that cries wolf at 0.8 sigma gets ignored, then rots.
FLAG_SIGMA = 2.0
DEFAULT_HOURS = {"cut": 72.0, "rsi": 24.0}


def _iter_rows(paths):
    """Yield parsed rows from each path; .gz transparently. A missing/corrupt shard is
    skipped, never fatal — a guard that dies on one bad archive file is a dead guard."""
    import gzip
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            continue


def _parse_ts(d):
    ts = str(d.get("ts") or "")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _lane_of(d):
    strat = str(d.get("strategy") or "?")
    win = str(d.get("window") or d.get("window_size") or "?")
    act = str(d.get("action") or "?")
    side = "up" if act == "BUY_YES" else "down"
    return f"{strat.replace('_macro', '')}|{win}|{side}"


def _blocked_return(action, yes_price, no_price, outcome):
    """Return per $1 staked on the BLOCKED side, given the real resolution.

    Prefer the LOGGED counter-side price — book/spread can make no_price != 1-yes_price.
    """
    def _px(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if 0.0 < f < 1.0 else None

    yp, npx = _px(yes_price), _px(no_price)
    if action == "BUY_YES":
        price = yp if yp is not None else (1.0 - npx if npx is not None else None)
        won = outcome == "YES"
    else:
        price = npx if npx is not None else (1.0 - yp if yp is not None else None)
        won = outcome == "NO"
    if price is None:
        return None, None, None
    r = (1.0 / price - 1.0) if won else -1.0
    return (1 if won else 0), r, price


def _matches(reason, guard):
    if guard == "rsi":
        return reason == "rsi_hard_blocked"
    # cut: the live lane-cut family, discovered not hardcoded
    return reason.endswith("_disabled_lane")


def run_guard(guard, hours, limit, throttle, include_archive=False, archive_shards=1, verbose=True):
    if not os.path.isfile(REJECT):
        sys.stderr.write(f"blocked_band_guard[{guard}]: no reject log at {REJECT}\n")
        return []

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)

    # ── collect candidates in the window ───────────────────────────────────────
    # Sources: the live log, plus (opt-in) the most recent archive shards. Rotation
    # moves history out of the live file, so without the archive a freshly-rotated log
    # reads as "gate is quiet" for a full cycle. The `hours` filter below still applies
    # to every row from every source — archive access does NOT reopen era pooling.
    paths = [REJECT]
    if include_archive:
        import glob as _glob
        shards = sorted(
            _glob.glob(os.path.join(CAL, "archive", "rejected_candidates_archive_*.jsonl.gz")),
            reverse=True,
        )[:archive_shards]
        paths.extend(shards)
        if verbose and shards:
            print(f"  + reading {len(shards)} archive shard(s)")

    pending = []
    dropped_old = 0
    for d in _iter_rows(paths):
        reason = str(d.get("reason") or d.get("skip_reason") or "")
        if not _matches(reason, guard):
            continue
        ts = _parse_ts(d)
        if ts is not None and ts < cutoff:
            dropped_old += 1
            continue          # ⛔ era filter — the defect the old tripwire had
        mid = str(d.get("market_id") or "")
        if not mid:
            continue
        pending.append((mid, d, reason))

    # ⛔ DEDUPE BY (lane, market_id). The scanner re-rejects the SAME market every cycle,
    # so one blocked opportunity can appear dozens of times and all copies join to the
    # same resolution. Undeduped this manufactures impossible win rates — the first run
    # of this guard read doge|15m|up at 100% WR on "n=24". One market = one opportunity.
    # Keep the EARLIEST rejection per market: that is the price we would have paid.
    seen = {}
    for mid, d, reason in pending:
        k = (_lane_of(d), mid)
        prev = seen.get(k)
        if prev is None or str(d.get("ts") or "") < str(prev[1].get("ts") or ""):
            seen[k] = (mid, d, reason)
    dup_dropped = len(pending) - len(seen)
    pending = list(seen.values())

    # ⚠️ Only markets whose window has ALREADY ENDED can resolve. Sorting purely by
    # "newest" spent the entire API budget on candidates that cannot possibly have
    # resolved yet (measured: 120/120 unresolved, every one with market_end_ts in the
    # future). Drop the not-yet-expired first, THEN take the newest of what remains.
    not_expired = 0
    expired = []
    for mid, d, reason in pending:
        end = _parse_ts({"ts": d.get("market_end_ts")})
        if end is not None and end > now:
            not_expired += 1
            continue
        expired.append((mid, d, reason))
    pending = expired

    # Newest first, so a --limit spends the API budget on the most current settled tape.
    pending.sort(key=lambda x: str(x[1].get("ts") or ""), reverse=True)
    capped = 0
    if limit and len(pending) > limit:
        capped = len(pending) - limit
        pending = pending[:limit]

    # ── resolve against REAL Polymarket outcomes ───────────────────────────────
    try:
        sys.path.insert(0, REPO)
        from src.analysis.ghost_calibration import fetch_resolution
    except Exception as e:
        sys.stderr.write(f"blocked_band_guard[{guard}]: cannot import fetch_resolution: {e}\n")
        return []

    cache = {}
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "ret": 0.0, "px": [], "rets": [], "reasons": set()})
    unresolved = 0
    for mid, d, reason in pending:
        oc = fetch_resolution(mid, cache)
        if throttle and mid not in cache:
            time.sleep(throttle)
        if oc not in ("YES", "NO"):
            unresolved += 1
            continue
        won, r, price = _blocked_return(d.get("action"), d.get("yes_price"), d.get("no_price"), oc)
        if won is None:
            continue
        a = agg[_lane_of(d)]
        a["n"] += 1
        a["wins"] += won
        a["ret"] += r
        a["rets"].append(r)
        a["px"].append(price)
        a["reasons"].add(reason)

    # ── verdicts ──────────────────────────────────────────────────────────────
    rows = []
    for lane, a in agg.items():
        n = a["n"]
        wr = a["wins"] / n
        ev = a["ret"] / n
        mean_px = sum(a["px"]) / len(a["px"])
        # sample std of the per-candidate return, then the sigma of the mean
        if n > 1:
            var = sum((r - ev) ** 2 for r in a["rets"]) / (n - 1)
            # ZERO-VARIANCE CASE: every candidate resolved the same way, so the sample
            # gives no spread estimate and se collapses to 0 -> sigma printed as +0.0
            # even at EV -1.000 (seen live on sol|1h|down n=5). Add one Laplace
            # pseudo-observation at the opposite extreme so se is finite and
            # CONSERVATIVE rather than silently degenerate.
            if var <= 0.0:
                pseudo = a["rets"] + [-ev]
                m = sum(pseudo) / len(pseudo)
                var = sum((r - m) ** 2 for r in pseudo) / (len(pseudo) - 1)
            se = (var ** 0.5) / (n ** 0.5)
        else:
            se = float("inf")
        sigma = (ev / se) if se > 0 else 0.0

        if n < MIN_N:
            verdict = "insufficient"
        elif ev > FLAG_EV and sigma >= FLAG_SIGMA:
            # gate is blocking a band that made money held to resolution, at real significance
            verdict = "FLAG_RELEASE_REVIEW"
        elif ev > FLAG_EV:
            verdict = "accruing"        # positive but inside the noise — keep watching, do NOT act
        elif ev < -0.10:
            verdict = "gate_holding"
        else:
            verdict = "borderline"
        rows.append({
            "ts_utc": now.isoformat(), "guard": guard, "lane": lane,
            "window_hours": hours, "blocked_settled_n": n,  # deduped: distinct markets, not rejection rows
            "would_wr": round(wr, 4),
            "breakeven_wr": round(mean_px, 4),      # breakeven IS the price
            "would_beat_pts": round((wr - mean_px) * 100, 2),
            "would_ev_per_dollar": round(ev, 4),
            "sigma": round(sigma, 2),
            "reasons": sorted(a["reasons"]),
            "verdict": verdict, "mode": "shadow_release_guard",
        })

    os.makedirs(CAL, exist_ok=True)
    with open(OUT[guard], "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    if verbose:
        print(f"blocked_band_guard[{guard}] window={hours:.0f}h  resolved={sum(a['n'] for a in agg.values())} "
              f"| tried={len(pending)} unresolved={unresolved} "
              f"not_yet_expired={not_expired} pre_window={dropped_old} dup_dropped={dup_dropped}"
              + (f" ⚠️CAPPED_OUT={capped} (raise --limit)" if capped else ""))
        if not rows:
            print("  no settled blocked candidates in window (not an error — the gate may be quiet, "
                  "or the log was just rotated and needs a cycle to accumulate)")
        for r in sorted(rows, key=lambda x: x["would_ev_per_dollar"], reverse=True):
            print(f"  {r['lane']:18} n={r['blocked_settled_n']:>3} "
                  f"would-WR {r['would_wr']:.0%} vs breakeven {r['breakeven_wr']:.0%} "
                  f"(BEAT {r['would_beat_pts']:+.1f}pts) EV/$ {r['would_ev_per_dollar']:+.3f} "
                  f"{r['sigma']:+.1f}sig -> {r['verdict']}")
        flags = [r for r in rows if r["verdict"] == "FLAG_RELEASE_REVIEW"]
        if flags:
            print(f"  ⚠️ {len(flags)} lane(s) FLAGGED at >={FLAG_SIGMA}sig — the gate is blocking a +EV band. "
                  f"HUMAN decision; this is would-win inference, not realized P&L.")
        acc = [r for r in rows if r["verdict"] == "accruing"]
        if acc:
            print(f"  · {len(acc)} lane(s) positive but INSIDE THE NOISE (<{FLAG_SIGMA}sig) — "
                  f"accruing, not actionable: {', '.join(r['lane'] for r in acc)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guard", choices=["cut", "rsi", "both"], default="both")
    ap.add_argument("--hours", type=float, default=None, help="override the rolling window")
    ap.add_argument("--limit", type=int, default=400, help="max markets to resolve per guard")
    ap.add_argument("--throttle", type=float, default=0.05, help="sleep between API calls")
    ap.add_argument("--once", action="store_true", help="accepted for symmetry; this is always one pass")
    ap.add_argument("--include-archive", action="store_true",
                    help="also scan recent rotated shards (still time-filtered by --hours)")
    ap.add_argument("--archive-shards", type=int, default=1)
    args = ap.parse_args()

    guards = ["cut", "rsi"] if args.guard == "both" else [args.guard]
    total_flags = 0
    for g in guards:
        hours = args.hours if args.hours is not None else DEFAULT_HOURS[g]
        rows = run_guard(g, hours, args.limit, args.throttle,
                         include_archive=args.include_archive,
                         archive_shards=args.archive_shards)
        total_flags += sum(1 for r in rows if r["verdict"] == "FLAG_RELEASE_REVIEW")
        print()
    print(f"blocked_band_guard: {total_flags} lane(s) flagged for release review")
    print("⛔ neutral_bias (43% of rejects) and bleed_hour_sit_out are OUT OF SCOPE by "
          "prior adjudication — see this file's docstring before re-probing either.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
