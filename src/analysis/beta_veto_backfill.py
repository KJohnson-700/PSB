"""Reconstruct historical beta-veto eligibility from live trade history.

This module builds a derived dataset answering:
"Which rejected candidates would have been blocked by the global beta veto
at a given (max_mean, min_n) setting, based on the live lane posterior state
available at the time of rejection?"

Important scope:
- Uses only historical live trades from ``data/calibration/trades.jsonl``.
- Replays the same Beta(2,3) live update math as ``LaneCalibrator.record``.
- Does not attempt to replay later ghost-fed beta updates, because those do not
  exist at rejection time.
- Does not apply per-lane threshold overrides; this is for global beta-veto
  tuning only.
"""

from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from src.analysis.lane_calibration import PRIOR_A, PRIOR_B


@dataclass
class LaneBetaState:
    n: int = 0
    beta_a: float = PRIOR_A
    beta_b: float = PRIOR_B

    @property
    def beta_mean(self) -> float:
        total = self.beta_a + self.beta_b
        if total <= 0:
            return PRIOR_A / (PRIOR_A + PRIOR_B)
        return self.beta_a / total

    def snapshot(self) -> Dict[str, Any]:
        return {
            "n": int(self.n),
            "beta_a": round(float(self.beta_a), 6),
            "beta_b": round(float(self.beta_b), 6),
            "beta_mean": round(float(self.beta_mean), 6),
        }

    def record_trade(self, win: bool) -> None:
        if win:
            self.beta_a += 1.0
        else:
            self.beta_b += 1.0
        self.n += 1


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rejected_live_lane_id(row: Dict[str, Any]) -> str:
    live_lane_id = str(row.get("live_lane_id") or "").strip()
    if live_lane_id:
        return live_lane_id
    context = row.get("context") or {}
    if isinstance(context, dict):
        return str(context.get("calibration_lane_id") or "").strip()
    return ""


def _ghost_id(row: Dict[str, Any]) -> str:
    key = f"{row.get('ts','')}|{row.get('market_id','')}|{row.get('reason','')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _load_trade_events(trades_path: Path) -> List[Tuple[datetime, str, bool]]:
    out: List[Tuple[datetime, str, bool]] = []
    for row in _iter_jsonl(trades_path):
        lane_id = str(row.get("lane_id") or "").strip()
        ts = _parse_ts(row.get("closed_at") or row.get("ts"))
        win = row.get("win")
        if not lane_id or ts is None or not isinstance(win, bool):
            continue
        out.append((ts, lane_id, win))
    out.sort(key=lambda item: item[0])
    return out


def _load_rejected_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    source: str,
) -> List[Tuple[datetime, Dict[str, Any]]]:
    out: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        live_lane_id = _rejected_live_lane_id(row)
        if ts is None or not live_lane_id:
            continue
        enriched = dict(row)
        enriched["_source"] = source
        enriched["_live_lane_id"] = live_lane_id
        out.append((ts, enriched))
    out.sort(key=lambda item: item[0])
    return out


def build_beta_veto_backfill(
    *,
    trades_path: Path,
    rejected_path: Path,
    settled_path: Path,
    beta_veto_max_mean: float,
    beta_veto_min_n: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    trades = _load_trade_events(trades_path)
    settled_by_id = {
        str(row.get("ghost_id") or ""): row
        for row in _iter_jsonl(settled_path)
        if row.get("ghost_id")
    }
    rejected_rows = list(_iter_jsonl(rejected_path))
    timeline = _load_rejected_rows(rejected_rows, source="rejected")

    state_by_lane: Dict[str, LaneBetaState] = {}
    trade_idx = 0
    annotated_rows: List[Dict[str, Any]] = []
    by_lane: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "rows": 0,
            "settled_rows": 0,
            "wins": 0,
            "losses": 0,
            "strategies": set(),
        }
    )
    by_strategy: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"rows": 0, "settled_rows": 0, "wins": 0, "losses": 0}
    )

    for reject_ts, row in timeline:
        while trade_idx < len(trades) and trades[trade_idx][0] <= reject_ts:
            _, lane_id, win = trades[trade_idx]
            state = state_by_lane.setdefault(lane_id, LaneBetaState())
            state.record_trade(win)
            trade_idx += 1

        live_lane_id = str(row["_live_lane_id"])
        state = state_by_lane.get(live_lane_id, LaneBetaState())
        beta_mean = state.beta_mean
        would_veto = state.n >= beta_veto_min_n and beta_mean < beta_veto_max_mean
        if not would_veto:
            continue

        ghost_id = str(row.get("ghost_id") or _ghost_id(row))
        settled = settled_by_id.get(ghost_id)
        strategy = str(row.get("strategy") or "").strip()
        annotated = {
            "ts": reject_ts.isoformat(),
            "source": str(row.get("_source") or ""),
            "ghost_id": ghost_id or None,
            "strategy": strategy or None,
            "window": row.get("window"),
            "action": row.get("action"),
            "reason": row.get("reason") or row.get("gate_reason"),
            "market_id": row.get("market_id"),
            "market_question": row.get("market_question"),
            "live_lane_id": live_lane_id,
            "ghost_lane_id": row.get("ghost_lane_id") or row.get("lane_id"),
            "beta_veto_setting": {
                "max_mean": float(beta_veto_max_mean),
                "min_n": int(beta_veto_min_n),
            },
            "posterior_before_reject": state.snapshot(),
            "would_beta_veto": True,
            "settled": bool(settled),
            "outcome": settled.get("outcome") if settled else None,
            "win": settled.get("win") if settled else None,
            "realized_pct": settled.get("realized_pct") if settled else None,
            "settled_at": settled.get("settled_at") if settled else None,
        }
        annotated_rows.append(annotated)

        lane_stats = by_lane[live_lane_id]
        lane_stats["rows"] += 1
        lane_stats["strategies"].add(strategy)
        strat_stats = by_strategy[strategy or "unknown"]
        strat_stats["rows"] += 1
        if settled:
            lane_stats["settled_rows"] += 1
            strat_stats["settled_rows"] += 1
            if settled.get("win") is True:
                lane_stats["wins"] += 1
                strat_stats["wins"] += 1
            elif settled.get("win") is False:
                lane_stats["losses"] += 1
                strat_stats["losses"] += 1

    def _lane_summary() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for lane_id, stats in by_lane.items():
            settled_n = int(stats["settled_rows"])
            wins = int(stats["wins"])
            losses = int(stats["losses"])
            out.append(
                {
                    "live_lane_id": lane_id,
                    "rows": int(stats["rows"]),
                    "settled_rows": settled_n,
                    "wins": wins,
                    "losses": losses,
                    "settled_win_rate": round(wins / settled_n, 6) if settled_n else None,
                    "strategies": sorted(str(s) for s in stats["strategies"] if s),
                }
            )
        out.sort(key=lambda row: (int(row["rows"]), int(row["settled_rows"])), reverse=True)
        return out

    by_strategy_out = []
    for strategy, stats in by_strategy.items():
        settled_n = int(stats["settled_rows"])
        wins = int(stats["wins"])
        by_strategy_out.append(
            {
                "strategy": strategy,
                "rows": int(stats["rows"]),
                "settled_rows": settled_n,
                "wins": wins,
                "losses": int(stats["losses"]),
                "settled_win_rate": round(wins / settled_n, 6) if settled_n else None,
            }
        )
    by_strategy_out.sort(key=lambda row: (int(row["rows"]), int(row["settled_rows"])), reverse=True)

    settled_rows = [row for row in annotated_rows if row["settled"]]
    settled_n = len(settled_rows)
    settled_wins = sum(1 for row in settled_rows if row.get("win") is True)
    summary = {
        "beta_veto_setting": {
            "max_mean": float(beta_veto_max_mean),
            "min_n": int(beta_veto_min_n),
        },
        "method": {
            "source": "historical_live_trade_replay",
            "trade_prior": {"beta_a": PRIOR_A, "beta_b": PRIOR_B},
            "notes": [
                "Derived from live trades only, replayed in timestamp order.",
                "Ghost-fed beta updates are excluded because they are not available at rejection time.",
                "Per-lane threshold overrides are excluded; this backfill covers the global beta-veto sweet-spot only.",
            ],
        },
        "counts": {
            "trade_events_replayed": len(trades),
            "rejected_rows_scanned": len(rejected_rows),
            "rows_matching_beta_veto": len(annotated_rows),
            "settled_rows_matching_beta_veto": settled_n,
            "settled_wins": settled_wins,
            "settled_losses": settled_n - settled_wins,
            "settled_win_rate": round(settled_wins / settled_n, 6) if settled_n else None,
        },
        "by_strategy": by_strategy_out,
        "top_lanes": _lane_summary()[:25],
    }
    return annotated_rows, summary


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
