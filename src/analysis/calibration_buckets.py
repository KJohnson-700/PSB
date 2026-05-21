"""Stable bucket tags for calibration logs and post-merge reporting."""

from __future__ import annotations

from typing import Any, Dict, Optional


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def edge_bucket(edge: Any) -> str:
    val = _coerce_float(edge)
    if val is None:
        return ""
    if val < 0.02:
        return "lt_0.02"
    if val < 0.05:
        return "0.02_0.05"
    if val < 0.08:
        return "0.05_0.08"
    if val < 0.12:
        return "0.08_0.12"
    if val < 0.18:
        return "0.12_0.18"
    return "ge_0.18"


def entry_price_bucket(price: Any) -> str:
    val = _coerce_float(price)
    if val is None:
        return ""
    if val < 0.43:
        return "lt_0.43"
    if val < 0.46:
        return "0.43_0.46"
    if val < 0.49:
        return "0.46_0.49"
    if val <= 0.51:
        return "0.49_0.51"
    if val <= 0.54:
        return "0.51_0.54"
    if val <= 0.57:
        return "0.54_0.57"
    return "gt_0.57"


def correlation_bucket(corr: Any) -> str:
    val = _coerce_float(corr)
    if val is None:
        return ""
    if val < 0.25:
        return "lt_0.25"
    if val < 0.5:
        return "0.25_0.50"
    if val < 0.7:
        return "0.50_0.70"
    return "ge_0.70"


def side_source_bucket(side_source: Any) -> str:
    text = str(side_source or "").strip().lower()
    if not text:
        return ""
    if "override" in text:
        return "override"
    if "btc" in text:
        return "btc_bias"
    if "macro" in text:
        return "macro_bias"
    if "observer" in text:
        return "observer"
    if "ai" in text:
        return "ai"
    return text


def regime_tag_bucket(regime: Any) -> str:
    text = str(regime or "").strip().lower()
    if not text:
        return ""
    if "__" in text:
        text = text.split("__", 1)[0]
    return text


def gate_family_bucket(gate_reason: Any, gate_stage: Any = None) -> str:
    for raw in (gate_reason, gate_stage):
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if "liquid" in text:
            return "liquidity"
        if "edge" in text:
            return "edge"
        if "follow" in text:
            return "follow"
        if "weak_confirm" in text:
            return "weak_confirm"
        if "hist_gate" in text or "histogram" in text:
            return "hist_gate"
        if "oracle" in text:
            return "oracle"
        if "entry_window" in text or "late_window" in text:
            return "entry_window"
        if "price_band" in text or "center_price" in text:
            return "price_band"
        if "ai_" in text or "ai-veto" in text or "ai_veto" in text:
            return "ai_veto"
        return text
    return ""


def rsi_bucket(rsi: Any) -> str:
    val = _coerce_float(rsi)
    if val is None:
        return ""
    if val < 35.0:
        return "low"
    if val <= 65.0:
        return "mid"
    return "high"


def atr_bucket(atr: Any, asset_spot: Any) -> str:
    """ATR as a fraction of spot — keeps the bucket comparable across assets.

    Bands: <0.5% spot = low (calm), 0.5–1.5% = mid, >1.5% = high (volatile).
    Returns "" when either ATR or spot is missing/non-positive.
    """
    a = _coerce_float(atr)
    s = _coerce_float(asset_spot)
    if a is None or s is None or s <= 0.0 or a < 0.0:
        return ""
    pct = a / s
    if pct < 0.005:
        return "low"
    if pct <= 0.015:
        return "mid"
    return "high"


def build_bucket_tags(
    *,
    edge: Any = None,
    yes_price: Any = None,
    correlation: Any = None,
    side_source: Any = None,
    regime_tag: Any = None,
    gate_reason: Any = None,
    gate_stage: Any = None,
    rsi: Any = None,
    atr: Any = None,
    asset_spot: Any = None,
) -> Dict[str, str]:
    return {
        "edge_bucket": edge_bucket(edge),
        "entry_price_bucket": entry_price_bucket(yes_price),
        "correlation_bucket": correlation_bucket(correlation),
        "side_source_bucket": side_source_bucket(side_source),
        "regime_tag_bucket": regime_tag_bucket(regime_tag),
        "gate_family_bucket": gate_family_bucket(gate_reason, gate_stage),
        "rsi_bucket": rsi_bucket(rsi),
        "atr_bucket": atr_bucket(atr, asset_spot),
    }
