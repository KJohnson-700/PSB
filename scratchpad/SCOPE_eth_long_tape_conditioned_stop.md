# SCOPE — eth-LONG tape-conditioned stop guard (per-lane, side-isolated)

## Problem (from live audit, sess test_20260804_034213)
`updown_stop_loss` = 68% of exits / −70.60. Split on `hold_minus_exit_pnl`:
- **eth LONG stops CUT WINNERS** — holdΔ **+54.81**. eth 5m/1h BUY_YES (+ several 5m BUY_NO) stopped on a dip in a bullish tape, then recovered. Holding eth's stopped trades = −40.81 → **+14.00**. 8/15 eth stops were confirmed held-winners (+23.47, +16.59, +14.07, +13.67, +8.50, +8.00…).
- xrp stops were CORRECT (holdΔ −20.39 = wrong-side direction). **Out of scope — different fault, different lane.**

## North-Star fit
Tape-CONDITIONED exit that self-flips bull↔bear (memory: `feedback_adaptive_tape_map_not_tape_blind_gates`). NOT a static "widen eth stop." In a DOWN tape the eth LONG stops as normal; ONLY in a confirmed UP tape does it get room — and only to a loser floor. This is the edit-discipline R1 (edge=EXIT) lever, not entry.

## Design — one suppression flag on the existing %-stop `elif`
Mirror the existing `_late_stop_suppressed` gate (live_testing.py:836) and the hold-means-hold loser-floor (1135). Add `_tape_hold_suppressed`, computed just above the stop `elif` (line 920):

```
_tape_hold_suppressed = False
if (is_updown and self._tape_hold_enabled and _latest_tape_state is not None
        and entry_leg != "NO"):                         # LONG/BUY_YES only
    lane = f"{strategy_name}:BUY_YES"                    # e.g. "eth_macro:BUY_YES"
    cfg = self._tape_hold_by_lane.get(lane)
    if cfg is not None and pnl_pct > -abs(cfg["floor_pct"]):   # LOSER FLOOR: only while shallow
        tm = _latest_tape_state(strategy_name) or {}
        _dir  = str(tm.get("direction") or "").upper()
        _conf = float(tm.get("confidence", 0.0) or 0.0)
        _dscore = int(tm.get("dscore", 0) or 0)
        _m1h = (tm.get("macd_signs") or [0,0,0])[2]      # 1h MACD sign
        _age = now - float(tm.get("ts", 0.0) or 0.0)
        if (_dir == "UP" and _conf >= cfg["conf_min"] and _age <= cfg["max_age_s"]
                and _dscore >= 2 and _m1h >= cfg["require_1h_macd"]):   # HTF-confirmed
            _tape_hold_suppressed = True
```

Then on the stop `elif` (line 923), add `and not _tape_hold_suppressed` next to the existing `and not _late_stop_suppressed`.

### Why the HTF-confirmation clause (`dscore>=2` + `macd_1h>=+1`)
Blended tape accuracy is 47% @5m / 52% @60m — a raw 5m UP read is a coinflip and must NOT defer a stop. What actually saved these trades was a *persistent* bullish regime (market turned up ~4am, stayed up hours). `dscore>=2` requires ≥2 of {macd-consensus, ema-stack, trend-label} agreeing UP, and `macd_1h>=+1` forces the 1-hour MACD to be up — i.e. defer a short-TF eth LONG's stop only when the HOUR agrees. This is the knob that separates "shallow dip inside an up-hour" (hold) from "5m noise" (stop).

## Loser floor (the non-negotiable safety — memory: holdmeanshold_no_loser_floor_catastrophic)
Suppression applies ONLY while `pnl_pct > -floor_pct` (seed 0.15). Past −15% the stop fires regardless of tape. The existing −50% catastrophic backstop is untouched. Worst case per trade: eth BUY_YES loses ~15% notional instead of ~8-10%; the audit shows current eth BUY_YES stops fire at −3…−7 on small notional and would have WON, so EV is positive as long as HTF-confirmed-UP beats coinflip on eth continuation (the 1h clause is what buys that).

## Config (restart-class — new `self._` attrs read in exit loop)
```yaml
tape_hold_stop:
  enabled: true
  by_lane:
    "eth_macro:BUY_YES":
      floor_pct: 0.15        # suppress %-stop only while loss shallower than -15%
      conf_min: 0.60         # tape confidence
      max_age_s: 90.0        # tape freshness
      require_1h_macd: 1      # 1h MACD must be UP (+1); set 0 to drop the HTF clause
```
Keyed `strategy:BUY_YES` (covers eth 5m AND 1h BUY_YES — both showed the leak). eth-only to start; add sol/xrp LONG later only on their own live evidence.

## What it does NOT touch
- flatten_pre_resolution (fires earlier `if`, still protects the resolution gap)
- take_profit / take_profit_late (fire before the stop `elif`)
- near-expiry time_stop (the `else` branch still runs when suppressed — position still managed into resolution)
- BUY_NO / SHORT lanes, all non-eth lanes, hold-to-resolution lanes (unaffected)

## Validation (ghosts DON'T cover exit changes — CLAUDE.md; backtests broken)
LIVE forward only: after deploy, on eth BUY_YES `updown_stop_loss` exits, `hold_minus_exit_pnl` should shrink toward 0 (stops stop cutting winners) and eth realized should climb. Watch for a new suppression when eth tape is UP; confirm floor fires when a deferred LONG breaches −15%. Optional log-only line each suppression (mark, dir, conf, dscore, m1h) for the offline join.

## Risk ledger
1. Tape misleads (UP but keeps falling) → floor caps at −15%. Bounded.
2. 5m tape weak → HTF clause (`dscore>=2`+`m1h>=1`) is the mitigant; if it still over-holds, raise `require_1h_macd` stays 1 and lift `conf_min`.
3. Adds an exit-path branch → restart-class, needs Codex review before deploy.

## Deploy gate
Codex review → operator GO → restart (ASK first, announce worklist, diff full tree vs HEAD, --paper). No hot-reload (frozen `__init__` attrs).
```
