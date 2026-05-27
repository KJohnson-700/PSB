"""Replay validator for the per-(asset, timeframe) lane direction FSM.

Walks recorded paper sessions in ``data/paper_trades/*/entries.jsonl`` and
computes what the new FSM would have decided per trade, given the posteriors
present in the snapshot file. Reports WR / pnl / sit-out volume per strategy,
session, and globally — then checks the acceptance bar from the plan:

  - On the 5/26 sessions, FSM-routed WR must be >= actual WR + 5pp.
  - On the 5/22 GOLD sessions, FSM-routed WR must not drop by more than 2pp.
  - In all cases, NEUTRAL/sit-out volume <= 25% of original trades.

The validator only inspects EXIT events (each is the matching close to one
ENTRY). For each EXIT, the script tries to reconstruct a minimal
``SOLTechnicalAnalysis``-shaped object from fields recorded on the entry —
specifically the per-asset MACD signs and RSI/EMA snapshots in ``extra``. If a
session does not carry enough state to reconstruct, that trade is excluded from
the FSM column (but still counted in actual). The percentage reconstructable is
reported per session.

Usage:
    python scripts/lane_direction_fsm_replay.py
    python scripts/lane_direction_fsm_replay.py --sessions test_20260522_052210 test_20260526_042005

This script is read-only — it does not mutate any state file. It explicitly
constructs an isolated FSM with ``state_path`` pointing at a temp file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Allow running as a script from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis import lane_direction_fsm as fsm_mod
from src.analysis.lane_direction_fsm import LaneDirectionFSM


# Default sessions to evaluate. The 5/22 GOLD sessions are the baseline the
# FSM must not break; the 5/26 sessions are the regressed period the FSM must
# improve.
DEFAULT_BASELINE_SESSIONS = ["test_20260521_212905", "test_20260522_052210"]
DEFAULT_CURRENT_SESSIONS = ["test_20260525_231430", "test_20260526_042005"]


# ────────────────────────────────────────────────────────────────────────────
# Minimal TA reconstruction
# ────────────────────────────────────────────────────────────────────────────

class _MACDStub:
    __slots__ = ("histogram", "histogram_rising", "above_zero", "crossover")

    def __init__(self, hist=0.0, rising=False, above_zero=False, crossover="NONE"):
        self.histogram = float(hist or 0.0)
        self.histogram_rising = bool(rising)
        self.above_zero = bool(above_zero)
        self.crossover = str(crossover or "NONE")


class _AssetStub:
    """Holds the SOLAnalysis-like attributes the FSM reads from ``ta.sol``."""

    def __init__(self):
        self.ema_9 = 0.0
        self.ema_21 = 0.0
        self.ema_50 = 0.0
        self.rsi_14 = 50.0
        self.macd_5m = _MACDStub()
        self.macd_15m = _MACDStub()
        self.macd_30m = _MACDStub()
        self.macd_1h = _MACDStub()
        self.macd_4h = _MACDStub()


class _TAStub:
    def __init__(self):
        self.sol = _AssetStub()


def _ta_from_entry(entry: Dict[str, Any]) -> Optional[_TAStub]:
    """Reconstruct a minimal TA stub from the ENTRY record's
    ``extra.indicator_snapshot`` block.

    Field map observed in entries.jsonl:
      - bitcoin strategy: ``btc_4h_histogram``, ``btc_1h_histogram``,
        ``btc_15m_histogram`` (+ ``_rising`` flag on each).
      - alt strategies (eth/sol/xrp/hype/bnb/doge): ``alt_5m_histogram``,
        ``alt_15m_histogram``, ``alt_1h_histogram`` (+ ``_rising``).
      - All entries log RSI as ``extra.rsi``.

    For the FSM the asset-under-strategy maps to ``ta.sol.*``: the bitcoin
    strategy points ``ta.sol`` at BTC data; each alt strategy points it at
    that alt's data. So we route ``btc_*`` snapshots for the bitcoin strategy
    and ``alt_*`` snapshots for everything else.

    Returns None if zero MACD timeframes are recoverable.
    """
    extra = entry.get("extra") or {}
    snap = extra.get("indicator_snapshot") or {}
    if not snap:
        return None

    asset = (entry.get("strategy") or "").lower()
    src_prefix = "btc" if asset == "bitcoin" else "alt"

    ta = _TAStub()
    recovered = 0
    for tf in ("5m", "15m", "30m", "1h", "4h"):
        h_key = f"{src_prefix}_{tf}_histogram"
        r_key = f"{src_prefix}_{tf}_histogram_rising"
        c_key = f"{src_prefix}_{tf}_crossover"
        a_key = f"{src_prefix}_{tf}_above_zero"
        if h_key not in snap:
            continue
        try:
            hf = float(snap[h_key])
        except (TypeError, ValueError):
            continue
        rising = bool(snap.get(r_key, False))
        # Fall back to (hf > 0) for older entries that don't log above_zero.
        above_zero = bool(snap.get(a_key, hf > 0)) if a_key in snap else (hf > 0)
        crossover = str(snap.get(c_key, "NONE") or "NONE")
        macd_obj = _MACDStub(
            hist=hf,
            rising=rising,
            above_zero=above_zero,
            crossover=crossover,
        )
        setattr(ta.sol, f"macd_{tf}", macd_obj)
        recovered += 1

    # EMA stack — newer entries log it as `{prefix}_ema_{n}`.
    for n in (9, 21, 50):
        v = snap.get(f"{src_prefix}_ema_{n}")
        if v is None:
            continue
        try:
            setattr(ta.sol, f"ema_{n}", float(v))
        except (TypeError, ValueError):
            pass

    # RSI: prefer the per-prefix field in snapshot; older entries fall back to
    # the legacy single `extra.rsi` (no _14 suffix).
    rsi_v = snap.get(f"{src_prefix}_rsi_14")
    if rsi_v is None:
        rsi_v = extra.get("rsi")
    if rsi_v is not None:
        try:
            ta.sol.rsi_14 = float(rsi_v)
        except (TypeError, ValueError):
            pass

    return ta if recovered >= 1 else None


def _htf_bias_from_entry(entry: Dict[str, Any]) -> str:
    extra = entry.get("extra") or {}
    for key in ("primary_htf_bias", "htf_bias", "btc_htf_bias", "alt_htf_bias"):
        v = (extra.get(key) or "").upper()
        if v in ("BULLISH", "BEARISH", "NEUTRAL"):
            return v
    return "NEUTRAL"


def _timeframe_from_entry(entry: Dict[str, Any]) -> Optional[str]:
    extra = entry.get("extra") or {}
    tf = extra.get("lane_window") or extra.get("window")
    if tf is None:
        return None
    tf = str(tf).lower().strip()
    if tf not in fsm_mod.VALID_TIMEFRAMES:
        return None
    return tf


def _action_taken(entry: Dict[str, Any]) -> Optional[str]:
    """Map a recorded entry's actual side to LONG/SHORT for diff purposes."""
    a = (entry.get("action") or entry.get("side") or "").upper()
    if "YES" in a:
        return "LONG"
    if "NO" in a:
        return "SHORT"
    return None


# ────────────────────────────────────────────────────────────────────────────
# Session loading
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionResult:
    name: str
    n_exits: int
    n_reconstructable: int
    actual_pnl: float
    actual_wins: int
    fsm_routes_to_actual: int          # FSM would have entered same side
    fsm_routes_opposite: int           # FSM would have flipped side → different counterfactual
    fsm_sit_out: int                   # FSM would have skipped
    fsm_recovery_entries: int          # FSM neutral-recovery slot would have fired
    fsm_pnl_estimate: float            # actual_pnl × (FSM_agreed) + flipped(−actual) + skipped(0)
    fsm_wins_estimate: int

    @property
    def actual_wr(self) -> float:
        if self.n_exits == 0:
            return 0.0
        return self.actual_wins / self.n_exits

    @property
    def fsm_wr(self) -> float:
        # Among trades the FSM would have admitted (agreed + opposite + recovery)
        admitted = self.fsm_routes_to_actual + self.fsm_routes_opposite + self.fsm_recovery_entries
        if admitted == 0:
            return 0.0
        return self.fsm_wins_estimate / admitted

    @property
    def sit_out_pct(self) -> float:
        if self.n_reconstructable == 0:
            return 0.0
        return self.fsm_sit_out / self.n_reconstructable


def _load_session_exits(session: str, root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (entries, exits) lists for a session. Both are time-ordered."""
    ep = root / "data" / "paper_trades" / session / "entries.jsonl"
    if not ep.exists():
        return [], []
    entries: List[Dict[str, Any]] = []
    exits: List[Dict[str, Any]] = []
    with ep.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = rec.get("event")
            if ev == "ENTRY":
                entries.append(rec)
            elif ev == "EXIT":
                exits.append(rec)
    return entries, exits


def _pair_entry_for_exit(exit_rec: Dict[str, Any], entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the matching ENTRY for an EXIT by trade_id."""
    tid = exit_rec.get("trade_id")
    if not tid:
        return None
    for e in entries:
        if e.get("trade_id") == tid:
            return e
    return None


# ────────────────────────────────────────────────────────────────────────────
# Per-session evaluation
# ────────────────────────────────────────────────────────────────────────────

def evaluate_session(session: str, root: Path, fsm: LaneDirectionFSM) -> SessionResult:
    entries, exits = _load_session_exits(session, root)
    res = SessionResult(
        name=session, n_exits=len(exits), n_reconstructable=0,
        actual_pnl=0.0, actual_wins=0,
        fsm_routes_to_actual=0, fsm_routes_opposite=0,
        fsm_sit_out=0, fsm_recovery_entries=0,
        fsm_pnl_estimate=0.0, fsm_wins_estimate=0,
    )
    if not exits:
        return res

    for ex in exits:
        pnl = float(ex.get("pnl", 0.0) or 0.0)
        won = pnl > 0
        res.actual_pnl += pnl
        if won:
            res.actual_wins += 1

        entry = _pair_entry_for_exit(ex, entries)
        if entry is None:
            continue
        ta = _ta_from_entry(entry)
        tf = _timeframe_from_entry(entry)
        if ta is None or tf is None:
            continue
        res.n_reconstructable += 1

        htf = _htf_bias_from_entry(entry)
        actual_side = _action_taken(entry)
        if actual_side is None:
            continue
        directive = fsm.resolve(
            asset=(entry.get("strategy") or "").lower(),
            timeframe=tf,
            ta=ta,
            htf_bias=htf,
            persist=False,
            write_audit=False,
        )

        if directive.side == "SIT_OUT":
            res.fsm_sit_out += 1
            continue
        if directive.size_multiplier < 1.0 and directive.side in ("LONG", "SHORT", "SIGNAL_TIME"):
            # Recovery slot. Conservative: count as same outcome as actual,
            # but scaled by the size_multiplier (it would have traded smaller).
            res.fsm_recovery_entries += 1
            if directive.side == actual_side or directive.side == "SIGNAL_TIME":
                if won:
                    res.fsm_wins_estimate += 1
                res.fsm_pnl_estimate += pnl * directive.size_multiplier
            else:
                # Opposite side at recovery size → invert outcome estimate
                if not won:
                    res.fsm_wins_estimate += 1
                res.fsm_pnl_estimate += (-pnl) * directive.size_multiplier
            continue

        # Directional admit at full size.
        if directive.side == actual_side:
            res.fsm_routes_to_actual += 1
            if won:
                res.fsm_wins_estimate += 1
            res.fsm_pnl_estimate += pnl
        else:
            res.fsm_routes_opposite += 1
            # Counterfactual: same trade taken the other way settles the
            # opposite. We invert pnl and win flag. This is approximate — TP/SL
            # geometry isn't symmetric — but it's the best deterministic
            # estimate from settled data alone.
            if not won:
                res.fsm_wins_estimate += 1
            res.fsm_pnl_estimate += -pnl
    return res


# ────────────────────────────────────────────────────────────────────────────
# Reporting + acceptance
# ────────────────────────────────────────────────────────────────────────────

def _fmt_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _print_session_table(rows: List[SessionResult]) -> None:
    print()
    print(f"{'session':35s} {'n':>5s} {'recon':>6s} {'actual_pnl':>12s} {'actual_WR':>10s} {'fsm_pnl':>10s} {'fsm_WR':>8s} {'sit-out':>8s}")
    print("-" * 110)
    for r in rows:
        print(f"{r.name:35s} {r.n_exits:5d} {r.n_reconstructable:6d} "
              f"{r.actual_pnl:+12.2f} {_fmt_pct(r.actual_wr):>10s} "
              f"{r.fsm_pnl_estimate:+10.2f} {_fmt_pct(r.fsm_wr):>8s} "
              f"{_fmt_pct(r.sit_out_pct):>8s}")


def _check_acceptance(
    baseline: List[SessionResult],
    current: List[SessionResult],
) -> Tuple[bool, List[str]]:
    """Returns (passed, list of failure reasons)."""
    failures: List[str] = []
    # 5/22 GOLD baseline: WR must not drop by more than 2pp on average.
    if baseline:
        b_actual = sum(r.actual_wr * r.n_reconstructable for r in baseline)
        b_fsm = sum(r.fsm_wr * (r.n_reconstructable - r.fsm_sit_out) for r in baseline)
        b_n_actual = sum(r.n_reconstructable for r in baseline)
        b_n_fsm = sum(r.n_reconstructable - r.fsm_sit_out for r in baseline)
        if b_n_actual > 0 and b_n_fsm > 0:
            b_wr_actual = b_actual / b_n_actual
            b_wr_fsm = b_fsm / b_n_fsm
            delta = b_wr_fsm - b_wr_actual
            print(f"\nBaseline (5/22) WR — actual={_fmt_pct(b_wr_actual)}  "
                  f"fsm={_fmt_pct(b_wr_fsm)}  Δ={delta * 100:+.1f}pp")
            if delta < -0.02:
                failures.append(
                    f"Baseline WR regressed by {-delta * 100:.1f}pp (>2pp threshold)"
                )
    # 5/26 current: FSM WR must beat actual WR by >= 5pp AND sit-out <= 25%.
    if current:
        c_actual = sum(r.actual_wr * r.n_reconstructable for r in current)
        c_fsm = sum(r.fsm_wr * (r.n_reconstructable - r.fsm_sit_out) for r in current)
        c_n_actual = sum(r.n_reconstructable for r in current)
        c_n_fsm = sum(r.n_reconstructable - r.fsm_sit_out for r in current)
        c_sit = sum(r.fsm_sit_out for r in current)
        c_total = sum(r.n_reconstructable for r in current)
        if c_n_actual > 0 and c_n_fsm > 0:
            c_wr_actual = c_actual / c_n_actual
            c_wr_fsm = c_fsm / c_n_fsm
            delta = c_wr_fsm - c_wr_actual
            sit_out_pct = (c_sit / c_total) if c_total else 0
            print(f"Current  (5/26) WR — actual={_fmt_pct(c_wr_actual)}  "
                  f"fsm={_fmt_pct(c_wr_fsm)}  Δ={delta * 100:+.1f}pp  "
                  f"sit-out={_fmt_pct(sit_out_pct)}")
            if delta < 0.05:
                failures.append(
                    f"Current WR improvement only {delta * 100:+.1f}pp (need >=+5pp)"
                )
            if sit_out_pct > 0.25:
                failures.append(
                    f"Sit-out volume {sit_out_pct * 100:.1f}% (cap 25%)"
                )
    return (len(failures) == 0, failures)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions", nargs="+", default=None,
        help="Override session list (default uses plan-pinned baseline + current sets).",
    )
    parser.add_argument(
        "--baseline-only", action="store_true",
        help="Evaluate only the 5/22 baseline set.",
    )
    parser.add_argument(
        "--current-only", action="store_true",
        help="Evaluate only the 5/26 current set.",
    )
    args = parser.parse_args(argv)

    # Build an isolated FSM that does NOT mutate any production state file.
    tmpdir = Path(tempfile.mkdtemp(prefix="lane_direction_replay_"))
    fsm = LaneDirectionFSM(
        state_path=tmpdir / "state.json",
        audit_path=tmpdir / "audit.jsonl",
        # Posteriors point at the real file — that's the calibration as of now.
        posteriors_path=REPO_ROOT / "data" / "calibration" / "lane_posteriors.json",
    )

    if args.sessions:
        # Single combined evaluation with whatever the user passed.
        baseline = []
        current = [evaluate_session(s, REPO_ROOT, fsm) for s in args.sessions]
    else:
        baseline = [] if args.current_only else [
            evaluate_session(s, REPO_ROOT, fsm) for s in DEFAULT_BASELINE_SESSIONS
        ]
        current = [] if args.baseline_only else [
            evaluate_session(s, REPO_ROOT, fsm) for s in DEFAULT_CURRENT_SESSIONS
        ]

    if baseline:
        print("=== BASELINE (5/22 GOLD) ===")
        _print_session_table(baseline)
    if current:
        print("\n=== CURRENT (5/26) ===")
        _print_session_table(current)

    passed, failures = _check_acceptance(baseline, current)
    print()
    if passed:
        print("ACCEPTANCE: PASS")
        return 0
    print("ACCEPTANCE: FAIL")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
