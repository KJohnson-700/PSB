"""Shared lane identity helpers for live journals and calibration reports."""

from __future__ import annotations

import re
from typing import Any, Optional


def _clean_part(value: Any, *, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    text = text.replace("/", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_:-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or default


def resolve_lane_side(
    *,
    action: Optional[str] = None,
    direction: Optional[str] = None,
    entry_leg: Optional[str] = None,
) -> str:
    direction_clean = _clean_part(direction, default="")
    if direction_clean in {"up", "down"}:
        return direction_clean

    action_clean = _clean_part(action, default="")
    if action_clean == "buy_no":
        return "down"
    if action_clean in {"buy_yes", "sell_yes"}:
        if _clean_part(entry_leg, default="") == "no":
            return "down"
        return "up"
    return "unknown"


def resolve_lane_regime(
    *,
    htf_bias: Optional[str] = None,
    primary_htf_bias: Optional[str] = None,
    alt_htf_bias: Optional[str] = None,
    btc_1h_regime: Optional[str] = None,
) -> str:
    parts = []
    for value in (primary_htf_bias, alt_htf_bias, htf_bias, btc_1h_regime):
        clean = _clean_part(value, default="")
        if clean:
            parts.append(clean)
    return "__".join(parts) if parts else "unclassified"


def resolve_entry_family(
    *,
    side_source: Optional[str] = None,
    ai_used: bool = False,
    reason: Optional[str] = None,
    signal_reason: Optional[str] = None,
) -> str:
    source = _clean_part(side_source, default="")
    if source:
        if "override" in source:
            return "override"
        if "ai" in source:
            return "ai_review"

    if ai_used:
        return "ai_assisted"

    context = " ".join(str(x or "") for x in (reason, signal_reason)).lower()
    if "late_window" in context:
        return "late_window"
    if "spike_" in context:
        return "spike"
    if "drift_" in context:
        return "drift"
    if "predict window" in context:
        return "predict_window"
    return "standard"


def build_lane_metadata(
    *,
    strategy: str,
    window_size: Optional[str],
    action: Optional[str] = None,
    direction: Optional[str] = None,
    entry_leg: Optional[str] = None,
    side_source: Optional[str] = None,
    ai_used: bool = False,
    reason: Optional[str] = None,
    signal_reason: Optional[str] = None,
    htf_bias: Optional[str] = None,
    primary_htf_bias: Optional[str] = None,
    alt_htf_bias: Optional[str] = None,
    btc_1h_regime: Optional[str] = None,
    promotion_state: Optional[str] = None,
) -> dict[str, str]:
    lane_side = resolve_lane_side(action=action, direction=direction, entry_leg=entry_leg)
    lane_window = _clean_part(window_size, default="unknown")
    lane_regime = resolve_lane_regime(
        htf_bias=htf_bias,
        primary_htf_bias=primary_htf_bias,
        alt_htf_bias=alt_htf_bias,
        btc_1h_regime=btc_1h_regime,
    )
    entry_family = resolve_entry_family(
        side_source=side_source,
        ai_used=ai_used,
        reason=reason,
        signal_reason=signal_reason,
    )
    lane = {
        "lane_side": lane_side,
        "lane_window": lane_window,
        "lane_regime": lane_regime,
        "entry_family": entry_family,
        "promotion_state": _clean_part(promotion_state, default="paper"),
    }
    lane["lane_id"] = "|".join(
        [
            _clean_part(strategy, default="unknown"),
            lane_window,
            lane_side,
            lane_regime,
            entry_family,
        ]
    )
    return lane
