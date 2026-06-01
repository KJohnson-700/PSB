"""Lane-specific BUY_YES repair helpers.

This module only supports soft corrections: probability haircuts and min-edge
adders. It intentionally has no lane-disable or allowlist behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class BuyYesRepairResult:
    matched: bool
    rule_name: str
    lane_key: str
    estimated_prob: float
    edge: float
    effective_min_edge: float
    probability_haircut: float = 0.0
    min_edge_add: float = 0.0
    oracle_basis_min_edge_add: float = 0.0

    @property
    def reason_token(self) -> str:
        if not self.matched:
            return ""
        parts = [f"buy_yes_lane_repair={self.rule_name or self.lane_key}"]
        if self.probability_haircut > 0:
            parts.append(f"prob_haircut={self.probability_haircut:.3f}")
        if self.min_edge_add > 0:
            parts.append(f"min_edge_add={self.min_edge_add:.3f}")
        if self.oracle_basis_min_edge_add > 0:
            parts.append(f"basis_min_edge_add={self.oracle_basis_min_edge_add:.3f}")
        return ";".join(parts)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _rules(cfg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = cfg.get("buy_yes_lane_repair") if isinstance(cfg, Mapping) else None
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return []
    rules = raw.get("rules") or []
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, Mapping)]


def resolve_buy_yes_lane_repair(
    *,
    strategy_config: Mapping[str, Any],
    strategy: str,
    window_size: str,
    action: str,
    lane_side: str,
    entry_family: str,
    estimated_prob: float,
    yes_price: float,
    edge: float,
    effective_min_edge: float,
    oracle_basis_bps: Optional[float] = None,
) -> BuyYesRepairResult:
    lane_key = "|".join([_norm(strategy), _norm(window_size), _norm(entry_family)])
    base = BuyYesRepairResult(
        matched=False,
        rule_name="",
        lane_key=lane_key,
        estimated_prob=float(estimated_prob),
        edge=float(edge),
        effective_min_edge=float(effective_min_edge),
    )
    if _norm(action) != "buy_yes" or _norm(lane_side) != "up":
        return base

    for rule in _rules(strategy_config):
        if _norm(rule.get("window")) not in {"", _norm(window_size)}:
            continue
        if _norm(rule.get("entry_family")) not in {"", _norm(entry_family)}:
            continue

        probability_haircut = max(0.0, _as_float(rule.get("probability_haircut")))
        min_edge_add = max(0.0, _as_float(rule.get("min_edge_add")))
        basis_add = 0.0
        basis_abs = abs(_as_float(oracle_basis_bps, 0.0))
        basis_threshold = rule.get("oracle_basis_abs_bps_min")
        if basis_threshold is not None and basis_abs >= max(0.0, _as_float(basis_threshold)):
            basis_add = max(0.0, _as_float(rule.get("oracle_basis_min_edge_add")))

        adjusted_prob = max(0.0, min(1.0, float(estimated_prob) - probability_haircut))
        # BUY_YES edge is probability above market YES price. Preserve any
        # previously applied edge penalty by moving edge down by the same haircut.
        adjusted_edge = max(-1.0, float(edge) - probability_haircut)
        adjusted_min = float(effective_min_edge) + min_edge_add + basis_add
        return BuyYesRepairResult(
            matched=True,
            rule_name=str(rule.get("name") or lane_key),
            lane_key=lane_key,
            estimated_prob=adjusted_prob,
            edge=adjusted_edge,
            effective_min_edge=adjusted_min,
            probability_haircut=probability_haircut,
            min_edge_add=min_edge_add,
            oracle_basis_min_edge_add=basis_add,
        )

    return base
