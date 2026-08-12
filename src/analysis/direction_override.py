"""
Direction-override seam — lets an EXTERNAL decider pick the trade side, with the
quant resolver as fallback. Two deciders:

  * ``claude``  — a manual override file that Claude Code (via a terminal or the chat
                  session) writes. This is the operator-selected mechanism: human/Claude
                  in the loop, decided live.
  * ``ai``      — the model_factory engine (minimax_tape / qwen / ...) picks the side.
                  Wired but INERT in v1 (returns quant fallback) until the engine call is
                  added; the seam is complete so flipping it on later is config-only.

DESIGN — safety first (this rides on the live money path):
  * DEFAULT is quant. The ONLY way a side changes is ``mode != quant`` AND ``enforce: true``.
  * With ``enforce: false`` (SHADOW) the seam only LOGS what it *would* do and returns the
    quant side unchanged — so we score it before it drives a single trade.
  * Every step is fail-safe: a missing/stale/malformed override, a bad config, a read
    error — any of them returns the quant side untouched. This module must never raise
    into the strategy and never block a trade.

CONFIG (``config/settings.yaml`` top-level ``direction:`` block — hot-reloadable):

    direction:
      mode: quant            # quant | claude | ai
      enforce: false         # false = SHADOW (log only, never change the side)
      override_file: data/runtime/claude_direction_override.json
      max_age_sec: 900       # override staleness cutoff (belt; each entry also has its own ttl)
      override_when_quant_neutral: false   # let an override supply a side when quant sat out (None)
      min_conf: 0.0          # ignore overrides whose conf is below this

OVERRIDE FILE schema (written by ``scripts/claude_direction.py`` or by hand):

    {
      "sol_macro:15m": {"side": "SHORT", "conf": 0.8, "ts": 1733600000, "ttl": 900, "why": "..."},
      "hype_macro":    {"side": "LONG",  "conf": 0.7, "ts": 1733600000},    # asset-wide (any tf)
      "*":             {"side": "SHORT", "conf": 0.6, "ts": 1733600000}      # global default
    }

  Key resolution (most specific first): ``<asset>:<tf>`` -> ``<asset>`` -> ``*``.
  First key that exists AND is fresh wins. ``side`` is LONG / SHORT (case-insensitive);
  ``FLAT`` / ``NONE`` / ``SKIP`` means "sit out" (returns None).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_OVERRIDE_FILE = "data/runtime/claude_direction_override.json"
_DEFAULT_MAX_AGE_SEC = 900.0

# mtime-cached parse of the override file so we re-read only when it changes.
_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": {}}


def _load_overrides(path: str) -> Dict[str, Any]:
    """Read + parse the override JSON, cached by mtime. Fail-safe: {} on any problem."""
    try:
        st = os.stat(path)
    except OSError:
        # No file => no overrides. Reset cache so a later-created file is picked up.
        if _cache["path"] == path and _cache["data"]:
            _cache.update({"mtime": None, "data": {}})
        return {}
    if _cache["path"] == path and _cache["mtime"] == st.st_mtime:
        return _cache["data"]
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:  # malformed / partial write / permission
        logger.debug("direction_override: unreadable override file %s: %s", path, exc)
        data = {}
    _cache.update({"path": path, "mtime": st.st_mtime, "data": data})
    return data


def _norm_side(raw: Any) -> Optional[str]:
    """LONG/SHORT (normalized) or None for sit-out / unrecognized."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in ("LONG", "BUY_YES", "YES", "UP"):
        return "LONG"
    if s in ("SHORT", "BUY_NO", "NO", "DOWN"):
        return "SHORT"
    # FLAT / NONE / SKIP / anything else => sit out
    return None


def _entry_fresh(entry: Dict[str, Any], now: float, max_age: float) -> bool:
    ts = entry.get("ts")
    if ts is None:
        # No timestamp => treat as fresh (hand-written entries); the belt max_age can't
        # judge it, so we honor it. Prefer writing ts via the CLI.
        return True
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return False
    age = now - ts
    if age < 0:
        age = 0.0
    ttl = entry.get("ttl")
    try:
        ttl = float(ttl) if ttl is not None else max_age
    except (TypeError, ValueError):
        ttl = max_age
    return age <= min(ttl, max_age) if ttl and max_age else age <= (ttl or max_age)


def _lookup(
    overrides: Dict[str, Any], strategy: str, tf: Optional[str], now: float, max_age: float
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (entry, matched_key) for the most specific fresh override, else (None, None)."""
    keys = []
    if tf:
        keys.append(f"{strategy}:{tf}")
    keys.append(strategy)
    keys.append("*")
    for k in keys:
        entry = overrides.get(k)
        if isinstance(entry, dict) and _entry_fresh(entry, now, max_age):
            return entry, k
    return None, None


def resolve(strategy: str, tf: Optional[str], quant_side: Optional[str], config: Any) -> Optional[str]:
    """Entry point called from ``_bias_to_side``.

    Returns the side the strategy should use. With the default config (no ``direction``
    block, or mode=quant) this returns ``quant_side`` unchanged and does nothing.

    ``quant_side`` is the strategy's own resolved side ("LONG" / "SHORT" / None).
    """
    try:
        get = config.get if hasattr(config, "get") else (lambda k, d=None: d)
        dcfg = get("direction", {}) or {}
        if not isinstance(dcfg, dict):
            return quant_side
        mode = str(dcfg.get("mode", "quant") or "quant").lower()
        if mode == "quant":
            return quant_side
        # 2026-08-12 WINDOW ROUTING (item 2): the AI direction signal is only >coinflip at 1h
        # (qwen_vision 50.6%; 5m/15m direction is fee-negative). Route the override to
        # apply_windows ONLY — other windows fall through to quant/native + structural edges
        # (RSI-fade etc). Absent/empty => all windows (legacy behavior).
        _apply_wins = dcfg.get("apply_windows")
        if _apply_wins and tf is not None and str(tf) not in {str(w) for w in _apply_wins}:
            return quant_side

        path = str(dcfg.get("override_file", _DEFAULT_OVERRIDE_FILE) or _DEFAULT_OVERRIDE_FILE)
        max_age = float(dcfg.get("max_age_sec", _DEFAULT_MAX_AGE_SEC) or _DEFAULT_MAX_AGE_SEC)
        min_conf = float(dcfg.get("min_conf", 0.0) or 0.0)
        allow_neutral = bool(dcfg.get("override_when_quant_neutral", False))
        enforce = bool(dcfg.get("enforce", False))

        override_side: Optional[str] = None
        conf: Optional[float] = None
        matched_key: Optional[str] = None
        why: str = ""

        if mode == "claude":
            now = time.time()
            entry, matched_key = _lookup(_load_overrides(path), strategy, tf, now, max_age)
            if entry is not None:
                conf = entry.get("conf")
                try:
                    conf = float(conf) if conf is not None else None
                except (TypeError, ValueError):
                    conf = None
                if conf is None or conf >= min_conf:
                    override_side = _norm_side(entry.get("side"))
                    why = str(entry.get("why", ""))[:80]
        elif mode == "ai":
            # Seam reserved for the model_factory engine (minimax_tape / qwen / ...).
            # INERT in v1: no engine call yet, so we fall back to quant. Wiring the engine
            # here is the only change needed to turn on automated AI-drive.
            logger.debug("direction_override: mode=ai not yet wired; quant fallback strat=%s", strategy)
            return quant_side
        else:
            return quant_side

        # No usable override for this lane => quant.
        if override_side is None and not (allow_neutral and matched_key is not None):
            return quant_side
        # If quant sat out (None) and we're not allowed to trade on neutral => quant (None).
        if quant_side is None and not allow_neutral:
            return quant_side

        differs = override_side != quant_side
        applied = enforce and differs
        if differs:
            logger.info(
                "DIRECTION_OVERRIDE strat=%s tf=%s quant=%s override=%s conf=%s mode=%s "
                "key=%s enforce=%s applied=%s why=%s",
                strategy, tf, quant_side, override_side, conf, mode,
                matched_key, enforce, applied, why,
            )
        return override_side if applied else quant_side
    except Exception as exc:  # absolute belt — never raise into the strategy
        logger.debug("direction_override: fail-safe quant (strat=%s): %s", strategy, exc)
        return quant_side
