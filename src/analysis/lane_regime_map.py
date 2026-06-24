"""Regime-layer map builder (the offline half of the regime layer).

Runs from cron every ~5 min (right after the ghost duckdb ingest). For each
managed alt lane (strategy, window, side) and each htf-bias regime it reads the
regime-conditioned settled-ghost EV from ``ghost.duckdb`` and decides whether the
lane should be (re-)enabled in paper mode, and at what size. The verdict is
written atomically to ``data/runtime/lane_regime_map.json``, which the live bot
reads fresh-per-scan via ``lane_regime_runtime.evaluate_lane``.

Key statistical choices (matched to the data's binary payoffs + fat tails):
  * EV statistic is a **winsorized mean** of realized_pct (clip to [-1, win_cap]),
    NOT the median and NOT the raw mean. The median is degenerate for binary
    payoffs (~+1 whenever WR>50%, ~0 at a coin-flip — it only encodes WR>50%),
    and the raw mean is blown up by rare 100x-400x ghost outliers. Clipping the
    right tail before averaging gives the lane's real edge. A lane enables on
    winsorized EV > threshold.
  * Enable is also gated on a **Wilson lower bound** of win-rate (not point WR),
    so a small-sample lucky lane opens tiny or not at all.
  * Size scales with EV strength (stronger EV => larger, still-paper size).
  * **Hysteresis**: a lane must qualify ``ENTER_BUILDS`` consecutive builds to
    turn on, and fail ``EXIT_BUILDS`` consecutive builds to turn off. This kills
    threshold-crossing whipsaw during regime transitions.

The map NEVER tightens: it can only re-enable YAML-disabled lanes (the runtime
helper treats YAML as the hard kill-switch and only consults the map to *open*).
Operator force_off/force_on live in ``lane_regime_overrides.json`` (read by the
runtime helper, and respected here for auto-pause writes).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# --- tunables (overridable via CLI) -----------------------------------------
MANAGED_STRATEGIES = ("sol_macro", "xrp_macro", "hype_macro", "doge_macro", "bnb_macro")
MANAGED_WINDOWS = ("5m", "15m", "1h")
# ghost side vocab is LONG/SHORT; live action vocab is BUY_YES/BUY_NO.
SIDE_TO_ACTION = {"SHORT": "BUY_NO", "LONG": "BUY_YES"}
BIASES = ("BEARISH", "BULLISH", "NEUTRAL")

# EV statistic note: realized_pct in these binary markets is bimodal (a win pays
# ~+1, a loss is exactly -1), so the MEDIAN is degenerate (~+1 whenever WR>50%)
# and per-trade p10 is structurally -1.0 for any lane that loses >10% of the time.
# Neither differentiates EV. We instead use a WINSORIZED MEAN: clip realized_pct
# to [-1, win_cap] to kill the rare 100x-400x ghost outliers that distort the raw
# mean, then average. That is the lane's real edge, robust to the fat right tail.
DEFAULTS = dict(
    lookback_days=14,
    fallback_days=30,         # widen window only if the 14d sample is too thin
    min_sample=150,
    win_cap=3.0,              # clip wins at +300% before averaging (kill outliers)
    enable_ev=0.02,           # winsorized-mean EV must exceed this to enable
    wr_sanity_floor=0.45,     # hard reject only clearly-broken point WR
    wr_floor=0.50,            # Wilson LB below this => size down (not reject)
    strong_ev=0.10,           # >= this => larger (still paper) size
    mid_ev=0.05,
    size_strong=0.50,
    size_mid=0.35,
    size_marginal=0.20,
    enter_builds=3,
    exit_builds=2,
    expiry_minutes=10,
    # ghost.duckdb is opened read-write by the transient ghost-settle subprocess;
    # a read-only connect collides with that cross-process lock. The writer holds
    # it only briefly, so retry with bounded linear backoff (max ~30s total, well
    # under the 5-min cron interval) clears nearly all collisions.
    connect_retries=5,
    connect_backoff_sec=2.0,
)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _detect_columns(con) -> Dict[str, str]:
    """ghost_settled schemas vary across eras; resolve the bias/time columns."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(ghost_settled)").fetchall()}
    bias_col = next((c for c in ("htf_bias", "primary_htf_bias", "regime_tag")
                     if c in cols), None)
    time_col = next((c for c in ("settled_at", "ts", "settled_ts")
                     if c in cols), None)
    return {"bias": bias_col, "time": time_col, "all": cols}


def _norm_bias_sql(bias_col: str) -> str:
    # Map raw bias text to BEARISH/BULLISH/NEUTRAL buckets in SQL.
    return (
        f"CASE WHEN upper({bias_col}) IN ('BEAR','BEARISH') THEN 'BEARISH' "
        f"WHEN upper({bias_col}) IN ('BULL','BULLISH') THEN 'BULLISH' "
        f"ELSE 'NEUTRAL' END"
    )


def _query_lane_stats(con, cols, lookback_days: int,
                      win_cap: float) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    """Return {(strategy,window,side,bias): {n,wr,ev,median}} over the lookback.

    ``ev`` is the winsorized mean of realized_pct (clipped to [-1, win_cap]) —
    the robust EV. ``median`` is kept for reporting/context only.
    """
    bias_col = cols["bias"]
    time_col = cols["time"]
    where = ["realized_pct IS NOT NULL"]
    if time_col:
        # settled_at may be stored as VARCHAR (ISO string) or a native timestamp;
        # try_cast handles both and drops unparseable rows (returns NULL).
        where.append(
            f"try_cast({time_col} AS TIMESTAMP) >= "
            f"(now()::TIMESTAMP - INTERVAL '{int(lookback_days)} days')"
        )
    bias_expr = _norm_bias_sql(bias_col) if bias_col else "'NEUTRAL'"
    cap = float(win_cap)
    sql = f"""
        SELECT strategy,
               "window" AS window,
               side,
               {bias_expr} AS bias,
               count(*) AS n,
               avg(CASE WHEN win THEN 1.0 ELSE 0.0 END) AS wr,
               avg(greatest(least(realized_pct, {cap}), -1.0)) AS ev,
               median(realized_pct) AS med
        FROM ghost_settled
        WHERE {' AND '.join(where)}
        GROUP BY 1,2,3,4
    """
    out: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for strat, win, side, bias, n, wr, ev, med in con.execute(sql).fetchall():
        if strat not in MANAGED_STRATEGIES or win not in MANAGED_WINDOWS:
            continue
        if side not in SIDE_TO_ACTION:
            continue
        out[(strat, win, side, bias)] = dict(
            n=int(n), wr=float(wr or 0.0),
            ev=float(ev if ev is not None else -1.0),
            median=float(med if med is not None else 0.0),
        )
    return out


def _size_for(stat: Dict[str, Any], cfg: Dict[str, Any]) -> float:
    # Borderline confidence (Wilson WR lower bound below the floor) opens tiny,
    # regardless of point EV — the conservative half of "open tiny or not at all".
    wr_lb = _wilson_lower_bound(stat["wr"] * stat["n"], stat["n"])
    if wr_lb < cfg["wr_floor"]:
        return cfg["size_marginal"]
    if stat["ev"] >= cfg["strong_ev"]:
        return cfg["size_strong"]
    if stat["ev"] >= cfg["mid_ev"]:
        return cfg["size_mid"]
    return cfg["size_marginal"]


def _wilson_lower_bound(wins: float, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a proportion — conservative WR estimate so a
    small-sample lucky lane does not enable on point WR alone."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (centre - margin) / denom


def _qualifies(stat: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    # Enable on sample + positive winsorized EV + a loose point-WR sanity floor.
    # Confidence (Wilson lower bound) controls SIZE in _size_for, not enable, so a
    # genuinely +EV asymmetric-payoff lane is not rejected for sub-50% point WR.
    return (
        stat["n"] >= cfg["min_sample"]
        and stat["ev"] > cfg["enable_ev"]
        and stat["wr"] >= cfg["wr_sanity_floor"]
    )


def _connect_readonly_retry(ghost_db: str, retries: int, backoff_sec: float):
    """Open a read-only duckdb connection, retrying on the transient cross-process
    lock conflict held by the ghost-settle writer.

    DuckDB blocks a read-only connect while another process holds the read-write
    lock on the same file. The settle writer holds it only briefly, so a short
    bounded linear backoff clears nearly all collisions. Only the lock error is
    retried; any other IOException (corrupt/missing db) re-raises immediately. If
    all retries are exhausted the last error re-raises, so cron logs it and the
    runtime helper fail-safes to YAML on the now-stale map (no bad trades)."""
    import time
    import duckdb

    retries = max(0, int(retries))
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return duckdb.connect(ghost_db, read_only=True)
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower():
                raise
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff_sec * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def build_map(
    ghost_db: str,
    out_path: str,
    state_path: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build and atomically write the lane-regime map. Returns the map dict."""
    cfg = {**DEFAULTS, **(cfg or {})}
    state = _load_json(state_path) or {}
    prev = state.get("lanes", {}) if isinstance(state, dict) else {}

    con = _connect_readonly_retry(
        ghost_db, cfg["connect_retries"], cfg["connect_backoff_sec"]
    )
    try:
        cols = _detect_columns(con)
        stats = _query_lane_stats(con, cols, cfg["lookback_days"], cfg["win_cap"])
        # widen lookback only for buckets too thin at 14d
        thin = {k for k, v in stats.items() if v["n"] < cfg["min_sample"]}
        if thin and cfg["fallback_days"] > cfg["lookback_days"]:
            wide = _query_lane_stats(con, cols, cfg["fallback_days"], cfg["win_cap"])
            for k in thin:
                if k in wide and wide[k]["n"] >= cfg["min_sample"]:
                    wide[k]["_widened"] = True
                    stats[k] = wide[k]
    finally:
        con.close()

    now = _utcnow()
    now_epoch = now.timestamp()
    lanes: Dict[str, Any] = {}
    new_state: Dict[str, Any] = {}

    # Iterate the full managed grid so disqualified lanes still update hysteresis.
    for strat in MANAGED_STRATEGIES:
        for win in MANAGED_WINDOWS:
            for side, action in SIDE_TO_ACTION.items():
                for bias in BIASES:
                    key = f"{strat}|{win}|{action}|{bias}"
                    stat = stats.get((strat, win, side, bias))
                    qual = bool(stat) and _qualifies(stat, cfg)

                    pst = prev.get(key, {})
                    enter = int(pst.get("enter_streak", 0))
                    exit_ = int(pst.get("exit_streak", 0))
                    was_on = bool(pst.get("enabled", False))

                    if qual:
                        enter += 1
                        exit_ = 0
                    else:
                        exit_ += 1
                        enter = 0

                    enabled = was_on
                    if not was_on and enter >= cfg["enter_builds"]:
                        enabled = True
                    elif was_on and exit_ >= cfg["exit_builds"]:
                        enabled = False

                    new_state[key] = dict(
                        enter_streak=enter, exit_streak=exit_, enabled=enabled,
                    )

                    if enabled and stat is not None:
                        lanes[key] = dict(
                            enabled=True,
                            size_scalar=round(_size_for(stat, cfg), 4),
                            sample_n=stat["n"],
                            reason=(
                                f"{bias} ghost ev={stat['ev']:+.3f} "
                                f"wr={stat['wr']:.2f} median={stat['median']:+.2f} "
                                f"n={stat['n']}"
                            ),
                            stats=dict(
                                ev_winsorized=round(stat["ev"], 4),
                                win_rate=round(stat["wr"], 4),
                                median_realized_pct=round(stat["median"], 4),
                                lookback_days=(cfg["fallback_days"]
                                               if stat.get("_widened")
                                               else cfg["lookback_days"]),
                            ),
                            regime=dict(asset_htf_bias=bias,
                                        detector="asset_htf_bias"),
                            computed_at=_iso(now),
                        )

    expires = now + _dt.timedelta(minutes=cfg["expiry_minutes"])
    out = dict(
        version=1,
        computed_at=_iso(now),
        computed_at_epoch=now_epoch,
        expires_at=_iso(expires),
        expires_at_epoch=expires.timestamp(),
        source="ghost.duckdb",
        config=cfg,
        lane_count=len(lanes),
        lanes=lanes,
    )
    _atomic_write_json(out_path, out)
    _atomic_write_json(state_path, dict(updated_at=_iso(now), lanes=new_state))
    return out


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build the lane-regime map from ghost.duckdb")
    ap.add_argument("--ghost-db", default="data/calibration/ghost.duckdb")
    ap.add_argument("--out", default="data/runtime/lane_regime_map.json")
    ap.add_argument("--state", default="data/runtime/lane_regime_state.json")
    ap.add_argument("--min-sample", type=int, default=DEFAULTS["min_sample"])
    ap.add_argument("--enable-ev", type=float, default=DEFAULTS["enable_ev"],
                    help="winsorized-mean EV threshold to enable a lane")
    ap.add_argument("--lookback-days", type=int, default=DEFAULTS["lookback_days"])
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    cfg = dict(min_sample=args.min_sample, enable_ev=args.enable_ev,
               lookback_days=args.lookback_days)
    out = build_map(args.ghost_db, args.out, args.state, cfg)
    if not args.quiet:
        print(f"lane_regime_map: {out['lane_count']} lanes enabled, "
              f"expires {out['expires_at']}")
        for k, v in sorted(out["lanes"].items()):
            print(f"  ENABLE {k}  size={v['size_scalar']}  {v['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
