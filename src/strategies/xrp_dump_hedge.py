"""
XRP 15m Up/Down — dump-and-hedge (quant only; no LLM).

Leg 1: Detect a sharp drop in YES mid vs a recent rolling peak on the Polymarket book (proxy for spot-driven repricing). Optionally require a BTC 5m
        log-return z-score ≤ -btc_z_min so the impulse is led by macro BTC.
Leg 2: When YES mid + NO mid ≤ max_pair_cost, buy NO to complete a discounted
        two-sided box (same structure as classic dump-and-hedge).

Live integration expects XRP 15m Gamma slugs (xrp-updown-15m-{ts}) and
``XRPDumpHedgeStrategy.markets_for_followup`` so the crypto cycle can keep
scanning a market where leg1 is already open (leg2 on the same market_id).
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field

from src.analysis.btc_price_service import BTCPriceService
from src.analysis.math_utils import PositionSizer
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market

logger = logging.getLogger(__name__)


def dump_triggered(
    peak_yes: float,
    yes_now: float,
    dump_move_frac: float,
) -> Tuple[bool, float]:
    """Return (triggered, drop_frac). drop_frac = (peak-yes)/peak."""
    if peak_yes < 0.08 or peak_yes <= 0:
        return False, 0.0
    drop = (peak_yes - yes_now) / peak_yes
    return drop >= dump_move_frac, drop


def hedge_pair_ok(yes: float, no: float, max_pair_cost: float) -> Tuple[bool, float]:
    """True when YES+NO is cheap enough to complete the box."""
    pair = yes + no
    return pair <= max_pair_cost + 1e-9, pair


XRP_PATTERNS = [
    re.compile(r"\bripple\b", re.IGNORECASE),
    re.compile(r"\bxrp\b", re.IGNORECASE),
]
UPDOWN_PATTERN = re.compile(
    r"(?:ripple|xrp)\s+up\s+or\s+down", re.IGNORECASE
)


def _market_window_minutes(market: Market) -> int:
    m = re.search(
        r"(\d+):(\d+)(AM|PM)[–\-](\d+):(\d+)(AM|PM)",
        market.question,
        re.IGNORECASE,
    )
    if m:
        h1, m1, p1, h2, m2, p2 = m.groups()
        h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
        if p1.upper() == "PM" and h1 != 12:
            h1 += 12
        if p1.upper() == "AM" and h1 == 12:
            h1 = 0
        if p2.upper() == "PM" and h2 != 12:
            h2 += 12
        if p2.upper() == "AM" and h2 == 12:
            h2 = 0
        start_min = h1 * 60 + m1
        end_min = h2 * 60 + m2
        diff = end_min - start_min
        if diff < 0:
            diff += 1440
        return diff
    q = market.question.lower()
    if "5m" in q or "5-min" in q:
        return 5
    return 15


class XRPDumpHedgeSignal(BaseModel):
    market_id: str
    market_question: str
    action: str  # BUY_YES (leg1) or BUY_NO (leg2)
    price: float
    size: float
    confidence: float = 0.5
    edge: float = 0.0
    token_id_yes: str
    token_id_no: str
    end_date: Optional[Any] = None
    strategy_name: str = "xrp_dump_hedge"
    leg: str = Field(..., description="1=dip buy YES, 2=hedge buy NO")
    reason: str = ""


@dataclass
class _PendingLeg1:
    peak_yes: float
    created_ts: float
    dollar_size: float


class XRPDumpHedgeStrategy:
    """Dump-and-hedge on XRP 15m up/down Polymarket books (no AI)."""

    def __init__(
        self,
        config: Dict[str, Any],
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: Optional[ExposureManager] = None,
        btc_service: Optional[BTCPriceService] = None,
    ):
        self.full_config = config
        self.config = config.get("strategies", {}).get("xrp_dump_hedge", {})
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer
        self.exposure_manager = exposure_manager or ExposureManager(config)
        self._btc = btc_service or BTCPriceService()
        self._signal_strategy_name = "xrp_dump_hedge"

        self.enabled = self.config.get("enabled", False)
        self.min_liquidity = float(self.config.get("min_liquidity", 1000))
        self.max_spread = float(self.config.get("max_spread", 0.06))
        self.dump_move_frac = float(self.config.get("dump_move_frac", 0.15))
        self.max_pair_cost = float(self.config.get("max_pair_cost", 0.95))
        self.history_maxlen = int(self.config.get("price_history_len", 48))
        self.use_btc_z_gate = bool(self.config.get("use_btc_z_gate", True))
        self.btc_z_min = float(self.config.get("btc_z_min", 2.0))
        self.kelly_fraction = float(self.config.get("kelly_fraction", 0.08))
        self.pending_ttl_sec = float(self.config.get("pending_ttl_sec", 900))

        self._yes_history: Dict[str, Deque[Tuple[float, float]]] = {}
        self._pending_leg1: Dict[str, _PendingLeg1] = {}
        # Backtests / unit tests: fixed z-score (None = fetch from Binance via BTC service).
        self._btc_z_override: Optional[float] = None

    def markets_for_followup(
        self,
        high_liquidity: List[Market],
        journal: Any,
        risk_manager: Any,
    ) -> List[Market]:
        """Market objects we must keep scanning (leg2 / pending) even if 'held'."""
        want: set = set(self._pending_leg1.keys())
        try:
            for pos in risk_manager.active_positions.values():
                if getattr(pos, "strategy", "") == "xrp_dump_hedge":
                    want.add(pos.market_id)
            for row in journal.get_open_positions():
                if row.get("strategy") == "xrp_dump_hedge":
                    want.add(row.get("market_id", ""))
        except Exception:
            pass
        want.discard("")
        return [m for m in high_liquidity if m.id in want]

    def _is_xrp_updown_15m(self, market: Market) -> bool:
        text = f"{market.question} {market.description}".lower()
        has_xrp = any(p.search(text) for p in XRP_PATTERNS)
        if not has_xrp or not UPDOWN_PATTERN.search(market.question):
            return False
        return _market_window_minutes(market) > 6

    def _record_yes(self, market_id: str, yes: float) -> None:
        dq = self._yes_history.setdefault(
            market_id, deque(maxlen=self.history_maxlen)
        )
        dq.append((time.time(), yes))

    def _rolling_peak_yes(self, market_id: str, horizon_sec: float) -> Optional[float]:
        dq = self._yes_history.get(market_id)
        if not dq:
            return None
        cutoff = time.time() - horizon_sec
        peak = None
        for ts, y in dq:
            if ts >= cutoff:
                peak = y if peak is None else max(peak, y)
        return peak

    def _btc_5m_return_zscore(self) -> Optional[float]:
        if not self.use_btc_z_gate:
            return 0.0
        if self._btc_z_override is not None:
            return float(self._btc_z_override)
        try:
            df = self._btc.fetch_klines("5m", 64)
            if df is None or len(df) < 25:
                return None
            close = df["close"].astype(float)
            r = np.log(close / close.shift(1)).dropna()
            if len(r) < 20:
                return None
            hist = r.iloc[:-1]
            sig = float(hist.std())
            if sig < 1e-9:
                return None
            z = float((r.iloc[-1] - hist.mean()) / sig)
            return z
        except Exception as e:
            logger.debug("XRP dump-hedge: BTC z-score unavailable: %s", e)
            return None

    def _size_usd(self, bankroll: float, edge: float) -> float:
        if self.kelly_sizer:
            return self.kelly_sizer.size_from_edge(self._signal_strategy_name, bankroll, edge)
        raw = bankroll * self.kelly_fraction * max(0.1, min(1.0, edge * 10))
        mn = self.full_config.get("trading", {}).get("default_position_size", 10)
        mx = self.full_config.get("trading", {}).get("max_position_size", 15)
        return float(max(mn, min(mx, raw)))

    async def scan_and_analyze(
        self, markets: List[Market], bankroll: float
    ) -> List[XRPDumpHedgeSignal]:
        if not self.enabled:
            return []

        out: List[XRPDumpHedgeSignal] = []
        z_btc = self._btc_5m_return_zscore()

        for market in markets:
            if not self._is_xrp_updown_15m(market):
                continue
            if market.liquidity < self.min_liquidity:
                continue
            if market.spread > self.max_spread:
                continue

            yes = float(market.yes_price)
            no = float(market.no_price)
            self._record_yes(market.id, yes)

            pair = yes + no
            pend = self._pending_leg1.get(market.id)
            if pend and (time.time() - pend.created_ts) > self.pending_ttl_sec:
                self._pending_leg1.pop(market.id, None)
                pend = None

            # ── Leg 2: complete hedge ─────────────────────────────────────
            if pend and pair <= self.max_pair_cost + 1e-6:
                edge = max(0.0, 1.0 - pair)
                usd = min(pend.dollar_size, self._size_usd(bankroll, edge))
                if usd > 0 and no > 0.01:
                    usd = self.exposure_manager.scale_size(usd)
                    out.append(
                        XRPDumpHedgeSignal(
                            market_id=market.id,
                            market_question=market.question,
                            action="BUY_NO",
                            price=min(0.99, no),
                            size=usd,
                            edge=edge,
                            confidence=0.65,
                            token_id_yes=market.token_id_yes,
                            token_id_no=market.token_id_no,
                            end_date=market.end_date,
                            leg="2",
                            reason=(
                                f"hedge pair={pair:.3f}<= {self.max_pair_cost} "
                                f"edge={edge:.3f}"
                            ),
                        )
                    )
                    self._pending_leg1.pop(market.id, None)
                continue

            if pend:
                # Still waiting for cheaper combined book
                continue

            # ── Leg 1: dump ────────────────────────────────────────────────
            peak = self._rolling_peak_yes(market.id, horizon_sec=180)
            if peak is None or peak < 0.08:
                continue
            drop_frac = (peak - yes) / peak if peak > 0 else 0.0
            if drop_frac < self.dump_move_frac:
                continue

            if self.use_btc_z_gate:
                if z_btc is None:
                    continue
                if z_btc > -self.btc_z_min:
                    continue

            edge1 = float(drop_frac)
            usd1 = self._size_usd(bankroll, edge1)
            if usd1 <= 0 or yes > 0.97:
                continue

            usd1 = self.exposure_manager.scale_size(usd1)

            out.append(
                XRPDumpHedgeSignal(
                    market_id=market.id,
                    market_question=market.question,
                    action="BUY_YES",
                    price=min(0.99, yes),
                    size=usd1,
                    edge=edge1,
                    confidence=0.55,
                    token_id_yes=market.token_id_yes,
                    token_id_no=market.token_id_no,
                    end_date=market.end_date,
                    leg="1",
                    reason=(
                        f"dump drop={drop_frac:.2%} peak={peak:.3f} yes={yes:.3f} "
                        f"btc_z={z_btc if z_btc is not None else 'n/a'}"
                    ),
                )
            )
            self._pending_leg1[market.id] = _PendingLeg1(
                peak_yes=peak,
                created_ts=time.time(),
                dollar_size=usd1,
            )

        return out
