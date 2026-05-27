"""Parameter-sweep tuner for the per-(asset, timeframe) lane direction FSM.

Drives the replay validator (lane_direction_fsm_replay) over a grid of
``t_enter``, ``t_exit``, ``htf_alpha``, and per-timeframe MACD weight emphasis.
Reports the top configurations by current-period WR improvement subject to the
acceptance constraints from the plan:

  - baseline (5/22 GOLD): WR drop <= 2pp
  - current  (5/26):      WR improvement >= 5pp AND sit-out <= 25%

The tuner does NOT write config — it prints the best configurations and the
recommended ``config/settings.yaml`` block to paste in once a candidate is
chosen. Operator stays in the loop on the parameter flip.

Usage:
    python scripts/lane_direction_fsm_tune.py
    python scripts/lane_direction_fsm_tune.py --quick     # coarse grid (~20 pts)
    python scripts/lane_direction_fsm_tune.py --top 10
"""

from __future__ import annotations

import argparse
import itertools
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis.lane_direction_fsm import (
    LaneDirectionFSM,
    DEFAULT_WEIGHTS_BY_TF,
)
from scripts.lane_direction_fsm_replay import (
    DEFAULT_BASELINE_SESSIONS,
    DEFAULT_CURRENT_SESSIONS,
    SessionResult,
    evaluate_session,
)


@dataclass
class TuneResult:
    t_enter: float
    t_exit: float
    htf_alpha: float
    macd_emphasis: str          # "default" | "macd_heavy" | "ema_heavy"
    n_ref: int
    baseline_wr_delta: float
    current_wr_delta: float
    current_sit_out_pct: float
    baseline_fsm_wr: float
    current_fsm_wr: float
    baseline_actual_wr: float
    current_actual_wr: float
    fsm_pnl_current: float
    passes: bool

    @property
    def score(self) -> float:
        """Rank: current_wr_delta is the main objective; baseline penalty if
        WR drops; sit-out over budget incurs penalty."""
        s = self.current_wr_delta
        if self.baseline_wr_delta < -0.02:
            s -= (abs(self.baseline_wr_delta) - 0.02) * 5  # strong penalty
        if self.current_sit_out_pct > 0.25:
            s -= (self.current_sit_out_pct - 0.25) * 5
        return s


def _make_weights(emphasis: str) -> Dict[str, Dict[str, float]]:
    """Generate per-tf weight presets to sweep."""
    if emphasis == "default":
        return DEFAULT_WEIGHTS_BY_TF
    if emphasis == "macd_heavy":
        # Bias toward MACD direction + momentum across all tf; suppress neighbour
        # and rsi (these are coarse fallbacks).
        out: Dict[str, Dict[str, float]] = {}
        for tf in DEFAULT_WEIGHTS_BY_TF:
            out[tf] = {
                "macd_direction": 0.40, "macd_momentum": 0.30,
                "macd_crossover": 0.15, "ema_alignment": 0.10,
                "rsi_zone": 0.02, "neighbor_tf": 0.03,
            }
        return out
    if emphasis == "ema_heavy":
        out = {}
        for tf in DEFAULT_WEIGHTS_BY_TF:
            out[tf] = {
                "macd_direction": 0.15, "macd_momentum": 0.15,
                "macd_crossover": 0.10, "ema_alignment": 0.45,
                "rsi_zone": 0.10, "neighbor_tf": 0.05,
            }
        return out
    if emphasis == "balanced_no_neighbour":
        # Many recorded entries do not include neighbour-tf MACD; suppressing
        # the neighbour contributor removes the contribution that's most
        # frequently missing.
        out = {}
        for tf in DEFAULT_WEIGHTS_BY_TF:
            out[tf] = {
                "macd_direction": 0.30, "macd_momentum": 0.25,
                "macd_crossover": 0.20, "ema_alignment": 0.15,
                "rsi_zone": 0.10, "neighbor_tf": 0.00,
            }
        return out
    raise ValueError(f"unknown emphasis: {emphasis}")


def _run_one(
    t_enter: float, t_exit: float, htf_alpha: float, emphasis: str, n_ref: int,
) -> TuneResult:
    tmpdir = Path(tempfile.mkdtemp(prefix="fsm_tune_"))
    fsm = LaneDirectionFSM(
        state_path=tmpdir / "state.json",
        audit_path=tmpdir / "audit.jsonl",
        posteriors_path=REPO_ROOT / "data" / "calibration" / "lane_posteriors.json",
        t_enter=t_enter, t_exit=t_exit, htf_alpha=htf_alpha,
        n_ref=n_ref, weights_by_tf=_make_weights(emphasis),
    )
    baseline = [evaluate_session(s, REPO_ROOT, fsm) for s in DEFAULT_BASELINE_SESSIONS]
    current = [evaluate_session(s, REPO_ROOT, fsm) for s in DEFAULT_CURRENT_SESSIONS]

    def _agg_wr(rows: List[SessionResult]) -> Tuple[float, float, float]:
        actual = sum(r.actual_wr * r.n_reconstructable for r in rows)
        n_actual = sum(r.n_reconstructable for r in rows) or 1
        fsm_admitted = sum(r.n_reconstructable - r.fsm_sit_out for r in rows)
        fsm_wins = sum(r.fsm_wins_estimate for r in rows)
        fsm_wr = (fsm_wins / fsm_admitted) if fsm_admitted else 0.0
        actual_wr = actual / n_actual
        return actual_wr, fsm_wr, actual_wr

    b_actual_wr, b_fsm_wr, _ = _agg_wr(baseline)
    c_actual_wr, c_fsm_wr, _ = _agg_wr(current)
    b_delta = b_fsm_wr - b_actual_wr
    c_delta = c_fsm_wr - c_actual_wr
    c_sit_pct = (
        sum(r.fsm_sit_out for r in current) /
        (sum(r.n_reconstructable for r in current) or 1)
    )
    fsm_pnl_current = sum(r.fsm_pnl_estimate for r in current)
    passes = (b_delta >= -0.02) and (c_delta >= 0.05) and (c_sit_pct <= 0.25)
    return TuneResult(
        t_enter=t_enter, t_exit=t_exit, htf_alpha=htf_alpha,
        macd_emphasis=emphasis, n_ref=n_ref,
        baseline_wr_delta=b_delta, current_wr_delta=c_delta,
        current_sit_out_pct=c_sit_pct,
        baseline_fsm_wr=b_fsm_wr, current_fsm_wr=c_fsm_wr,
        baseline_actual_wr=b_actual_wr, current_actual_wr=c_actual_wr,
        fsm_pnl_current=fsm_pnl_current, passes=passes,
    )


def _grid(quick: bool) -> List[Tuple[float, float, float, str, int]]:
    if quick:
        t_enters = [0.20, 0.30, 0.40]
        t_exits = [0.05, 0.15]
        alphas = [0.00, 0.15, 0.30]
        emphases = ["default", "macd_heavy", "balanced_no_neighbour"]
        n_refs = [200]
    else:
        t_enters = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]
        t_exits = [0.05, 0.10, 0.15, 0.20]
        alphas = [0.00, 0.10, 0.15, 0.20, 0.30]
        emphases = ["default", "macd_heavy", "ema_heavy", "balanced_no_neighbour"]
        n_refs = [100, 200, 400]
    out = []
    for te, tx, a, em, nr in itertools.product(t_enters, t_exits, alphas, emphases, n_refs):
        if tx >= te:    # T_exit must be < T_enter
            continue
        out.append((te, tx, a, em, nr))
    return out


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+5.1f}pp"


def _fmt_wr(x: float) -> str:
    return f"{x * 100:5.1f}%"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="Use a coarse grid (~30 pts) instead of full (~700).")
    parser.add_argument("--top", type=int, default=10,
                        help="Print top-N configurations by score.")
    parser.add_argument("--show-failing", action="store_true",
                        help="Include configurations that fail acceptance.")
    args = parser.parse_args(argv)

    grid = _grid(args.quick)
    print(f"Sweeping {len(grid)} configurations...")
    results: List[TuneResult] = []
    for i, (te, tx, a, em, nr) in enumerate(grid):
        try:
            r = _run_one(te, tx, a, em, nr)
        except Exception as exc:
            print(f"  [{i+1}/{len(grid)}] error: {exc}")
            continue
        results.append(r)
    print(f"Done. {sum(1 for r in results if r.passes)} configs pass acceptance.\n")

    passing = sorted([r for r in results if r.passes], key=lambda r: -r.score)
    failing = sorted([r for r in results if not r.passes], key=lambda r: -r.score)

    if passing:
        print("=== PASSING CONFIGURATIONS (top N by current-period WR gain) ===")
    else:
        print("=== NO CONFIGURATIONS PASS — showing top-by-score for inspection ===")
    print(f"{'t_enter':>8s} {'t_exit':>7s} {'alpha':>6s} {'emphasis':>22s} {'n_ref':>6s}"
          f"   {'baseΔ':>7s} {'curΔ':>7s} {'sit_out':>8s} {'cur_pnl':>9s}")
    print("-" * 100)
    show = passing if passing else failing
    for r in show[: args.top]:
        marker = "  " if r.passes else "✗ "
        print(f"{marker}{r.t_enter:6.2f} {r.t_exit:7.2f} {r.htf_alpha:6.2f} "
              f"{r.macd_emphasis:>22s} {r.n_ref:6d}   "
              f"{_fmt_pct(r.baseline_wr_delta):>7s} {_fmt_pct(r.current_wr_delta):>7s} "
              f"{_fmt_wr(r.current_sit_out_pct):>8s} {r.fsm_pnl_current:+9.2f}")

    if args.show_failing and passing:
        print("\n=== TOP FAILING CONFIGS (for context) ===")
        print(f"{'t_enter':>8s} {'t_exit':>7s} {'alpha':>6s} {'emphasis':>22s} {'n_ref':>6s}"
              f"   {'baseΔ':>7s} {'curΔ':>7s} {'sit_out':>8s} {'cur_pnl':>9s}")
        for r in failing[: args.top]:
            print(f"  {r.t_enter:6.2f} {r.t_exit:7.2f} {r.htf_alpha:6.2f} "
                  f"{r.macd_emphasis:>22s} {r.n_ref:6d}   "
                  f"{_fmt_pct(r.baseline_wr_delta):>7s} {_fmt_pct(r.current_wr_delta):>7s} "
                  f"{_fmt_wr(r.current_sit_out_pct):>8s} {r.fsm_pnl_current:+9.2f}")

    if passing:
        best = passing[0]
        print("\n=== RECOMMENDED config/settings.yaml VALUES (top result) ===")
        print(f"lane_direction_t_enter: {best.t_enter}")
        print(f"lane_direction_t_exit: {best.t_exit}")
        print(f"lane_direction_htf_alpha: {best.htf_alpha}")
        print(f"lane_direction_posterior_n_ref: {best.n_ref}")
        print(f"# contributor weights preset: {best.macd_emphasis}")
        if best.macd_emphasis != "default":
            print("lane_direction_contributor_weights:")
            for tf, w in _make_weights(best.macd_emphasis).items():
                print(f"  {tf}:")
                for k, v in w.items():
                    print(f"    {k}: {v}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
