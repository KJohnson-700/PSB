"""Per-lane threshold derivation from ghost + live outcomes.

The live ``LaneCalibrator`` applies global thresholds (β-veto floor 0.40,
min sample 30, α clamp [0.30, 2.50]) to every lane. Two complementary
input streams carry enough information to compute *per-lane* thresholds:

- ``rejected_candidates_settled.jsonl`` — would-have-been outcomes on
  every candidate the live scanner rejected. Catches lanes that *should*
  be opened (rejected pool > global floor) and lanes that should stay
  rejected (rejected pool < floor).
- ``trades.jsonl`` — actual outcomes on every accepted entry. Catches
  *selection bias inside accepted lanes*: a lane where the rejected pool
  has 70% WR can still bleed at 30% WR live if the bot's selection
  within it is anti-edge. Ghost data alone can never see this.

Outcomes from both streams are merged equal-weight by live ``lane_id``;
veto fires when combined n meets the floor AND combined WR is below the
threshold.

The combined results are written to ``lane_thresholds.json``. The live
calibrator loads them at boot and consults them at admission time. **Off
by default** — gated by ``lane_calibration.per_lane_thresholds.enabled``
config.

Schema of ``lane_thresholds.json``::

    {
      "schema_version": 1,
      "computed_at": "2026-05-23T09:30:00+00:00",
      "min_bucket_n": 100,
      "wr_veto_threshold": 0.40,
      "thresholds": {
        "sol_macro|5m|down|bearish__bearish__bull|standard": {
          "n": 612,
          "wr": 0.301,
          "ghost_n": 487,
          "ghost_wr": 0.269,
          "live_n": 125,
          "live_wr": 0.424,
          "veto_recommended": true,
          "recommended_max_mean": 0.40
        },
        ...
      }
    }
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.analysis.lane_identity import build_lane_metadata, clean_lane_part, compose_lane_id

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_SETTLED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates_settled.jsonl"
DEFAULT_TRADES_LOG = DEFAULT_CALIBRATION_DIR / "trades.jsonl"
DEFAULT_THRESHOLDS_PATH = DEFAULT_CALIBRATION_DIR / "lane_thresholds.json"

SCHEMA_VERSION = 1

# Defaults — operator overridable via config.
DEFAULT_MIN_BUCKET_N = 100
DEFAULT_WR_VETO_THRESHOLD = 0.40
# When a lane has enough ghost data AND ghost WR is below the threshold,
# recommend veto. The recommended_max_mean lets the operator dial how
# tight the override is — by default match the global floor.
DEFAULT_RECOMMENDED_MAX_MEAN = 0.40
# Once a lane has at least this many live (accepted) trades, the veto
# decision uses live data ALONE — ghost data is dropped from the
# decision. Rationale: ghost is the rejected-pool counterfactual; live
# is the accepted (selection-biased) subset. They estimate different
# distributions. While live n is small, the merge is informative; once
# live is mature, mixing in ghost masks selection drift inside the cell.
DEFAULT_LIVE_MATURE_N = 50

# Directional-flip recommendation. A lane is flagged ``flip_recommended`` when
# its chosen side reliably LOSES held-to-resolution at high sample — in a binary
# up/down market that means the OPPOSITE side reliably wins, so the lane should
# trade flipped rather than be vetoed. Stricter bar than the veto: more samples
# and a lower WR ceiling, and (when live PnL is known) only on lanes actually
# losing money. The flipped side still has to clear the normal edge gate.
DEFAULT_FLIP_MIN_N = 80
DEFAULT_FLIP_WR_MAX = 0.40
# Only strategies that actually consult flip_recommended at entry may flip; for
# everyone else flip must NOT pre-empt the veto (otherwise a losing lane in an
# unwired strategy would lose its veto AND not flip — trading unmanaged). The
# flip injection lives in the shared sol_macro scan loop, inherited by the
# sol-family. bitcoin and eth_macro have separate loops and are NOT wired.
DEFAULT_FLIP_STRATEGIES = frozenset(
    {"sol_macro", "xrp_macro", "hype_macro", "doge_macro", "bnb_macro"}
)


@dataclass
class LaneBucket:
    """Counterfactual WR aggregate for one live lane_id.

    ``pnl_sum`` is only populated by the live aggregator (real $ PnL
    from accepted trades). Ghost buckets leave it at 0.0 — they have
    hypothetical payouts but not the realized fills the veto cares about.
    """

    n: int = 0
    wins: int = 0
    pnl_sum: float = 0.0

    @property
    def losses(self) -> int:
        return self.n - self.wins

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.n) if self.n > 0 else None


def _ghost_to_live_lane_id(rec: Dict[str, Any]) -> Optional[str]:
    """Mirror of ``ghost_calibration._ghost_to_live_lane_keys`` returning the
    first translated key (or None if metadata insufficient).

    Mirrors ghost settlement: prefer the exact lane id when present, otherwise
    rebuild the live lane from the rejected-record metadata so new entry-family
    taxonomy is preserved for threshold learning.
    """
    live_lane_id = str(rec.get("live_lane_id") or "").strip()
    if live_lane_id and len(live_lane_id.split("|")) >= 5:
        return live_lane_id
    context = rec.get("context")
    if isinstance(context, dict):
        context_lane_id = str(context.get("calibration_lane_id") or "").strip()
        if context_lane_id and len(context_lane_id.split("|")) >= 5:
            return context_lane_id
    else:
        context = {}

    lid = str(rec.get("lane_id") or "")
    parts = lid.split("|")
    if len(parts) < 3:
        return None
    strategy = str(rec.get("strategy") or parts[0]).strip()
    window = str(rec.get("window") or parts[1]).strip()
    direction = str(parts[2] or "").strip()
    if not strategy or not window or not direction:
        return None

    primary_bias = str(
        rec.get("primary_htf_bias")
        or context.get("primary_htf_bias")
        or rec.get("htf_bias")
        or context.get("htf_bias")
        or ""
    ).strip()
    alt_bias = str(
        rec.get("alt_htf_bias")
        or context.get("alt_htf_bias")
        or ""
    ).strip()
    btc_bias = str(
        rec.get("btc_htf_bias")
        or rec.get("btc_1h_regime")
        or context.get("btc_1h_regime")
        or ""
    ).strip()
    if strategy != "bitcoin" and primary_bias and not alt_bias:
        alt_bias = primary_bias

    lane_meta = build_lane_metadata(
        strategy=strategy,
        window_size=window,
        direction=direction,
        side_source=rec.get("side_source") or context.get("side_source"),
        resolver_path=rec.get("resolver_path") or context.get("resolver_path"),
        ai_used=bool(context.get("ai_used")),
        reason=rec.get("reason"),
        signal_reason=context.get("signal_reason") or rec.get("reason"),
        htf_bias=(primary_bias if strategy == "bitcoin" else None),
        primary_htf_bias=(None if strategy == "bitcoin" else primary_bias),
        alt_htf_bias=(None if strategy == "bitcoin" else alt_bias),
        btc_1h_regime=(None if strategy == "bitcoin" else btc_bias),
    )
    lane_regime = str(lane_meta.get("lane_regime") or "").strip() or "unclassified"
    lane_family = ""
    for candidate in (
        rec.get("lane_family"),
        context.get("lane_family"),
        context.get("entry_family"),
        parts[4] if len(parts) >= 5 and parts[4] != "rejected" else "",
    ):
        lane_family = clean_lane_part(candidate, default="")
        if lane_family:
            break
    if not lane_family:
        lane_family = str(lane_meta.get("entry_family") or "").strip() or "standard"
    return compose_lane_id(
        strategy=strategy,
        window_size=window,
        lane_side=direction,
        lane_regime=lane_regime,
        entry_family=lane_family,
    )


def _iter_settled(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("lane_thresholds: failed to read %s: %s", path, exc)
        return


def aggregate_ghost_buckets(
    settled_path: Path = DEFAULT_SETTLED_LOG,
) -> Dict[str, LaneBucket]:
    """Aggregate ghost (rejected-and-settled) outcomes by translated live lane_id."""
    buckets: Dict[str, LaneBucket] = defaultdict(LaneBucket)
    for rec in _iter_settled(settled_path):
        win = rec.get("win")
        if not isinstance(win, bool):
            continue
        live_id = _ghost_to_live_lane_id(rec)
        if not live_id:
            continue
        b = buckets[live_id]
        b.n += 1
        if win:
            b.wins += 1
    return buckets


def aggregate_live_buckets(
    trades_path: Path = DEFAULT_TRADES_LOG,
) -> Dict[str, LaneBucket]:
    """Aggregate accepted-trade outcomes by ``lane_id`` straight from
    ``trades.jsonl``.

    Trades carry the canonical live ``lane_id`` already, so no translation
    is needed. Both shadow_mode and real-money entries are included —
    selection bias is the same path regardless. Phantom/no-fill rows
    (no boolean ``win``) are skipped.
    """
    buckets: Dict[str, LaneBucket] = defaultdict(LaneBucket)
    for rec in _iter_settled(trades_path):
        win = rec.get("win")
        if not isinstance(win, bool):
            continue
        lane_id = str(rec.get("lane_id") or "").strip()
        if not lane_id or len(lane_id.split("|")) < 5:
            continue
        b = buckets[lane_id]
        b.n += 1
        if win:
            b.wins += 1
        try:
            b.pnl_sum += float(rec.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pass
    return buckets


def compute_lane_thresholds(
    settled_path: Path = DEFAULT_SETTLED_LOG,
    *,
    trades_path: Path = DEFAULT_TRADES_LOG,
    min_bucket_n: int = DEFAULT_MIN_BUCKET_N,
    wr_veto_threshold: float = DEFAULT_WR_VETO_THRESHOLD,
    recommended_max_mean: float = DEFAULT_RECOMMENDED_MAX_MEAN,
    live_mature_n: int = DEFAULT_LIVE_MATURE_N,
    flip_min_n: int = DEFAULT_FLIP_MIN_N,
    flip_wr_max: float = DEFAULT_FLIP_WR_MAX,
    flip_strategies: frozenset = DEFAULT_FLIP_STRATEGIES,
) -> Dict[str, Any]:
    """Compute per-lane threshold recommendations from ghost + live data.

    Decision rule per lane:
      - If ``live_n >= live_mature_n``: use LIVE ONLY. Ghost is the
        rejected-pool counterfactual; live is the accepted (selection-
        biased) subset. Once live is statistically mature they are
        different distributions and mixing them masks the selection
        drift the veto needs to catch.
      - Else: combine equal-weight (``n = ghost_n + live_n``,
        ``wins = ghost_wins + live_wins``). Small live samples are too
        noisy to decide alone; ghost still informs.

    A lane gets ``veto_recommended=True`` if the decision-stream WR is
    below ``wr_veto_threshold`` AND the decision-stream sample is at
    least ``min_bucket_n``. The non-decision stream is still recorded
    in the output for review.
    """
    ghost = aggregate_ghost_buckets(settled_path)
    live = aggregate_live_buckets(trades_path)
    all_ids = set(ghost.keys()) | set(live.keys())
    thresholds: Dict[str, Dict[str, Any]] = {}
    for lane_id in all_ids:
        g = ghost.get(lane_id) or LaneBucket()
        l = live.get(lane_id) or LaneBucket()
        if l.n >= live_mature_n:
            decision_n = l.n
            decision_wins = l.wins
            decision_source = "live"
        else:
            decision_n = g.n + l.n
            decision_wins = g.wins + l.wins
            decision_source = "combined"
        if decision_n < min_bucket_n:
            continue
        decision_wr = decision_wins / decision_n
        wr_below_floor = decision_wr < wr_veto_threshold
        # Profitability guard: never veto a cell that is currently
        # making money. Polymarket payouts are uneven (NO at 0.65 pays
        # 0.35 per win, etc.), so a sub-40% WR cell can still be net
        # positive. PnL data is only on live trades; for combined/ghost-
        # only decisions we fall back to pure WR because we have no
        # realized PnL to consult.
        live_pnl_positive = l.n > 0 and l.pnl_sum > 0.0
        live_pnl_negative = l.n > 0 and l.pnl_sum < 0.0
        # Flip takes precedence over veto: a strongly-inverted lane with enough
        # samples should trade the opposite side, not be killed. Require the
        # stricter sample floor + WR ceiling, and — when live PnL is known —
        # only flip lanes actually losing money (don't flip a low-WR cell that
        # is net positive on uneven payouts).
        # Require REAL live-money evidence: only flip lanes the bot actually
        # traded and lost on, at high sample. Ghost-only cells (l.n == 0, mostly
        # pre_resolver_reject identities that never match a live candidate) are
        # too weak to justify trading the opposite side and are excluded.
        lane_strategy = lane_id.split("|", 1)[0]
        flip = (
            lane_strategy in flip_strategies
            and decision_n >= flip_min_n
            and decision_wr <= flip_wr_max
            and live_pnl_negative
            and l.n >= flip_min_n
        )
        veto = wr_below_floor and not live_pnl_positive and not flip
        entry: Dict[str, Any] = {
            "n": int(decision_n),
            "wr": round(decision_wr, 4),
            "decision_source": decision_source,
            "ghost_n": int(g.n),
            "live_n": int(l.n),
            "veto_recommended": bool(veto),
            "flip_recommended": bool(flip),
            "recommended_max_mean": float(recommended_max_mean),
        }
        if g.n > 0:
            entry["ghost_wr"] = round(g.wins / g.n, 4)
        if l.n > 0:
            entry["live_wr"] = round(l.wins / l.n, 4)
            entry["live_pnl"] = round(l.pnl_sum, 2)
        if wr_below_floor and live_pnl_positive:
            entry["veto_suppressed_reason"] = "profitable_despite_low_wr"
        thresholds[lane_id] = entry
    return {
        "schema_version": SCHEMA_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "min_bucket_n": int(min_bucket_n),
        "wr_veto_threshold": float(wr_veto_threshold),
        "live_mature_n": int(live_mature_n),
        "flip_min_n": int(flip_min_n),
        "flip_wr_max": float(flip_wr_max),
        "thresholds": thresholds,
    }


def write_lane_thresholds(
    payload: Dict[str, Any],
    *,
    path: Path = DEFAULT_THRESHOLDS_PATH,
) -> bool:
    """Atomic-ish write of lane_thresholds.json. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("lane_thresholds: write failed: %s", exc)
        return False


def load_lane_thresholds(
    path: Path = DEFAULT_THRESHOLDS_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Load per-lane thresholds from disk. Returns empty dict if file
    is missing or unparseable — calibrator must fall back to global
    defaults in that case."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("lane_thresholds: load failed: %s", exc)
        return {}
    if not isinstance(blob, dict):
        return {}
    if blob.get("schema_version") != SCHEMA_VERSION:
        logger.warning(
            "lane_thresholds: schema mismatch (have %s expected %s) — ignoring",
            blob.get("schema_version"), SCHEMA_VERSION,
        )
        return {}
    thresholds = blob.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return {}
    return thresholds


def summarize_thresholds(payload: Dict[str, Any]) -> str:
    """Human-readable summary for CLI / logs."""
    thresholds: Dict[str, Dict[str, Any]] = payload.get("thresholds", {})
    veto_rows = [
        (lid, info) for lid, info in thresholds.items() if info.get("veto_recommended")
    ]
    veto_rows.sort(key=lambda kv: (kv[1].get("ghost_wr") or 0))
    lines = [
        f"min_bucket_n={payload.get('min_bucket_n')}  "
        f"wr_veto_threshold={payload.get('wr_veto_threshold')}  "
        f"computed_at={payload.get('computed_at')}",
        f"total lanes with sufficient data: {len(thresholds)}",
        f"veto recommended: {len(veto_rows)}",
        "",
    ]
    veto_rows.sort(key=lambda kv: (kv[1].get("wr") if kv[1].get("wr") is not None else (kv[1].get("ghost_wr") or 0)))
    if veto_rows:
        lines.append(
            f"{'lane_id':<60} {'n':>5} {'wr':>6} {'g_n':>5} {'g_wr':>6} {'l_n':>5} {'l_wr':>6}"
        )
        for lid, info in veto_rows[:40]:
            n = info.get("n", info.get("ghost_n", 0))
            wr = info.get("wr", info.get("ghost_wr", 0)) or 0.0
            g_n = info.get("ghost_n", 0)
            g_wr = info.get("ghost_wr")
            l_n = info.get("live_n", 0)
            l_wr = info.get("live_wr")
            lines.append(
                f"{lid:<60} {n:>5d} {wr:>6.3f} "
                f"{g_n:>5d} {(g_wr if g_wr is not None else 0):>6.3f} "
                f"{l_n:>5d} {(l_wr if l_wr is not None else 0):>6.3f}"
            )
        if len(veto_rows) > 40:
            lines.append(f"  ... +{len(veto_rows)-40} more")
    return "\n".join(lines)
