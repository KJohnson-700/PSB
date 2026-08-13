#!/usr/bin/env python3
"""side_resolver_v2 — single-owner side resolver (DETERMINISTIC, observe-only shadow).

Per Codex design 2026-08-06. The champion resolver stacks override stages (native -> htf ->
fresh-cross -> fade -> window-delta -> quant-flip) with NO single owner; measured right-side% is
~coinflip (clean Binance-truth ledger: bot 50.8%, eth 43%). v2 produces ONE decision with ONE named
owner and an auditable precedence, so we can measure whether a single-owner policy beats the champion
on realized right-side% BEFORE any live wiring.

This module is PURE (features in -> decision out), no I/O, no trading side effects. The shadow daemon
(scripts/side_resolver_v2_shadow.py) feeds it candidate features and logs champion-vs-v2 for the
Binance-truth scorer. Nothing here changes live behavior.

Precedence (highest first) — justified by the clean ledger:
  1. lane_tape_adapter   — realized per-(asset,window,side) edge says one side clearly earns admission
  2. tape_map            — current tape directional AND not contradicted by realized adapter
  3. quant              — quant decisive AND tape not opposing (never BTC-driven for an alt)
  4. fresh_cross        — fresh own-asset cross, tape/adapter not opposing
  5. mean_reversion     — lane-approved fade only (RSI extreme, not unanimous trend)
  6. native             — fallback only (native owns 88% today but is ~coinflip -> demote to default)
  7. sit_out            — the only available side comes from known-bad disagreement -> abstain
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass(frozen=True)
class SideResolverFeatures:
    strategy: str
    asset: str
    window: str
    market_id: str
    native_bias: str = "NEUTRAL"          # BULLISH / BEARISH / NEUTRAL
    native_side: Optional[str] = None      # LONG / SHORT
    htf_bias: str = "NEUTRAL"
    htf_side: Optional[str] = None
    quant_side: Optional[str] = None
    momentum_side: Optional[str] = None
    fresh_cross_side: Optional[str] = None
    overbought: bool = False
    oversold: bool = False
    rsi_14: Optional[float] = None
    tape_dir: Optional[str] = None         # UP / DOWN / FLAT
    tape_conf: Optional[float] = None
    tape_adapter_up: float = 0.0           # realized edge for LONG on this lane
    tape_adapter_down: float = 0.0         # realized edge for SHORT on this lane
    champion_side: Optional[str] = None
    champion_side_source: str = ""
    champion_resolver_path: Optional[str] = None


@dataclass(frozen=True)
class SideResolverDecision:
    side: Optional[str]      # LONG / SHORT / None (abstain)
    owner: str               # single owner responsible
    confidence: float        # 0..1 ordinal (NOT a sizing input in phase 1)
    why: str


# tunables (kept explicit; a promotion pass can sweep these per-lane)
_ADAPTER_EDGE_MIN = 0.04       # realized adapter delta that "clearly earns" a side
_ADAPTER_DEADBAND = 0.01       # tape owner needs adapter not losing by more than this
_RSI_OVERBOUGHT = 68.0
_RSI_OVERSOLD = 32.0


def _dir_to_side(d: Optional[str]) -> Optional[str]:
    if d == "UP":
        return "LONG"
    if d == "DOWN":
        return "SHORT"
    return None


def resolve_side_v2(f: SideResolverFeatures) -> SideResolverDecision:
    up_edge, dn_edge = float(f.tape_adapter_up), float(f.tape_adapter_down)
    tape_side = _dir_to_side(f.tape_dir)

    # 1. lane_tape_adapter — realized edge clearly favors one side, the other doesn't
    if max(up_edge, dn_edge) >= _ADAPTER_EDGE_MIN and abs(up_edge - dn_edge) >= _ADAPTER_EDGE_MIN:
        side = "LONG" if up_edge > dn_edge else "SHORT"
        conf = min(1.0, 0.6 + abs(up_edge - dn_edge))
        return SideResolverDecision(side, "lane_tape_adapter", conf,
                                    f"adapter up={up_edge:+.3f} dn={dn_edge:+.3f}")

    # 2. tape_map — tape directional and realized adapter not contradicting it
    if tape_side and f.tape_dir in ("UP", "DOWN"):
        adapter_for = up_edge if tape_side == "LONG" else dn_edge
        adapter_against = dn_edge if tape_side == "LONG" else up_edge
        if adapter_for >= adapter_against - _ADAPTER_DEADBAND:
            conf = min(1.0, 0.55 + float(f.tape_conf or 0.0) * 0.3)
            return SideResolverDecision(tape_side, "tape_map", conf,
                                        f"tape={f.tape_dir} conf={f.tape_conf} adapter_ok")

    # 3. quant — decisive and tape not opposing; never BTC-derived for an alt
    if f.quant_side in ("LONG", "SHORT"):
        tape_opposes = tape_side is not None and tape_side != f.quant_side
        if not tape_opposes:
            return SideResolverDecision(f.quant_side, "quant", 0.55,
                                        f"quant={f.quant_side} tape_not_opposing")

    # 4. fresh_cross — fresh own-asset cross, tape/adapter not opposing
    if f.fresh_cross_side in ("LONG", "SHORT"):
        tape_opposes = tape_side is not None and tape_side != f.fresh_cross_side
        if not tape_opposes:
            return SideResolverDecision(f.fresh_cross_side, "fresh_cross", 0.52,
                                        f"fresh_cross={f.fresh_cross_side}")

    # 5. mean_reversion — lane-approved fade: RSI extreme, native trend to fade, not unanimous
    if f.native_side in ("LONG", "SHORT") and f.rsi_14 is not None:
        # fade a stretched move: overbought -> SHORT, oversold -> LONG
        if (f.overbought or f.rsi_14 >= _RSI_OVERBOUGHT) and f.native_side == "LONG":
            if not (f.htf_side == "LONG" and f.native_side == "LONG" and f.momentum_side == "LONG"):
                return SideResolverDecision("SHORT", "mean_reversion", 0.5,
                                            f"fade overbought rsi={f.rsi_14:.0f}")
        if (f.oversold or f.rsi_14 <= _RSI_OVERSOLD) and f.native_side == "SHORT":
            if not (f.htf_side == "SHORT" and f.native_side == "SHORT" and f.momentum_side == "SHORT"):
                return SideResolverDecision("LONG", "mean_reversion", 0.5,
                                            f"fade oversold rsi={f.rsi_14:.0f}")

    # 6. native — fallback (demoted from the champion's implicit law)
    if f.native_side in ("LONG", "SHORT"):
        # abstain when native disagrees with a decided HTF and nothing above rescued it —
        # the champion's htf_disagreement paths are the loss-heavy ones.
        if f.htf_side in ("LONG", "SHORT") and f.htf_side != f.native_side:
            return SideResolverDecision(None, "sit_out", 0.0,
                                        f"native={f.native_side} vs htf={f.htf_side} unrescued")
        return SideResolverDecision(f.native_side, "native", 0.45, "native fallback")

    # 7. sit_out
    return SideResolverDecision(None, "sit_out", 0.0, "no owner earned a side")
