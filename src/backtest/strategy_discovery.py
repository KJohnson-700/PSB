"""
Strategy-specific market discovery for backtesting.

Fade and arbitrage target different market types:
- Fade: Extreme consensus (price >= 95% or <= 5%) — contrarian, bet against crowd
- Arbitrage: Mispriced markets (AI prob vs market) — any price level with edge

Each strategy gets a universe filtered to markets it would actually consider.
"""
import logging
from typing import List, Tuple, Optional

import pandas as pd
import requests

from src.backtest.data_loader import PolymarketLoader

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Events known to have multiple markets with price history (2024)
CURATED_EVENTS_2024 = [
    "presidential-election-popular-vote-winner-2024",
    "republican-presidential-nominee-2024",
    "democratic-presidential-nominee-2024",
]


def _expand_market_slugs(events: list) -> List[str]:
    """Extract all market slugs from events."""
    slugs = []
    seen = set()
    for e in events:
        for m in e.get("markets") or []:
            s = m.get("slug") or ""
            if s and s not in seen:
                slugs.append(s)
                seen.add(s)
        ev_slug = e.get("slug") or ""
        if ev_slug and ev_slug not in seen and not e.get("markets"):
            slugs.append(ev_slug)
            seen.add(ev_slug)
    return slugs


def _fetch_candidate_slugs(
    start_date: str,
    end_date: str,
    limit: int,
    from_gamma: bool = True,
) -> List[str]:
    """Fetch candidate market slugs from Gamma API or curated events."""
    slugs = []
    seen = set()

    if from_gamma:
        start_ts = pd.Timestamp(start_date).value // 10**9
        end_ts = pd.Timestamp(end_date).value // 10**9
        offset = 2500 if pd.Timestamp(start_date).year >= 2024 else 0
        page_size = 100
        for _ in range(30):
            r = requests.get(
                f"{GAMMA_BASE}/events",
                params={"limit": page_size, "offset": offset, "closed": "true"},
                timeout=60,
            )
            r.raise_for_status()
            events = r.json()
            if not isinstance(events, list) or not events:
                break
            for e in events:
                end_str = (e.get("endDate") or "")[:10]
                start_str = (e.get("startDate") or e.get("creationDate") or "")[:10]
                if not end_str:
                    continue
                try:
                    ev_end = pd.Timestamp(end_str)
                    ev_start = pd.Timestamp(start_str) if start_str else ev_end
                except Exception:
                    continue
                if ev_end.value // 10**9 >= start_ts and ev_start.value // 10**9 <= end_ts:
                    for m in e.get("markets") or []:
                        s = m.get("slug") or ""
                        if s and s not in seen:
                            slugs.append(s)
                            seen.add(s)
                    if not e.get("markets"):
                        ev_slug = e.get("slug") or ""
                        if ev_slug and ev_slug not in seen:
                            slugs.append(ev_slug)
                            seen.add(ev_slug)
                if len(slugs) >= limit:
                    return slugs[:limit]
            offset += page_size
            if len(events) < page_size:
                break

    if len(slugs) < limit:
        for ev_slug in CURATED_EVENTS_2024:
            r = requests.get(f"{GAMMA_BASE}/events", params={"slug": ev_slug}, timeout=30)
            evs = r.json()
            if isinstance(evs, list) and evs:
                for s in _expand_market_slugs(evs):
                    if s not in seen:
                        slugs.append(s)
                        seen.add(s)
            if len(slugs) >= limit:
                break

    return slugs[:limit]


def discover_markets_for_strategy(
    strategy: str,
    start_date: str,
    end_date: str,
    loader: PolymarketLoader,
    min_bars: int = 50,
    target_count: int = 30,
    max_candidates: int = 80,
    max_bars: Optional[int] = 200,
    max_extreme_bars: Optional[int] = None,
) -> List[Tuple[str, pd.DataFrame]]:
    """
    Discover markets suitable for a given strategy.

    Fade: Only markets that had extreme consensus (price >= 0.95 or <= 0.05),
          with limited bars at extreme to avoid 100+ trades per market.
    Arbitrage: Markets with price movement away from 0.5 (mispricing potential).

    max_bars: Skip markets with more bars (avoids long series that over-trade).
    max_extreme_bars: For fade, max bars where price is extreme (caps trade count).
    """
    candidates = _fetch_candidate_slugs(start_date, end_date, max_candidates)
    results: List[Tuple[str, pd.DataFrame]] = []
    fade_threshold = 0.95
    arb_min_std = 0.03

    for slug in candidates:
        if len(results) >= target_count:
            break
        data = loader.load_market_data(slug, start_date, end_date, "1h")
        if data is None or len(data) < min_bars:
            continue

        prices = data["price"]
        spread = data.get("spread", pd.Series(0.02, index=data.index))
        max_spread = float(spread.max()) if hasattr(spread, "max") else 0.02

        if strategy == "fade":
            if prices.max() < fade_threshold and prices.min() > (1 - fade_threshold):
                continue
            # Only include markets where extreme consensus was brief (not 800+ bars)
            extreme_mask = (prices >= fade_threshold) | (prices <= (1 - fade_threshold))
            extreme_bars = int(extreme_mask.sum())
            if max_extreme_bars is not None and extreme_bars > max_extreme_bars:
                continue
            results.append((slug, data))
            logger.debug(f"Fade eligible: {slug[:50]} (extreme_bars={extreme_bars}, max={prices.max():.2f})")
        elif strategy == "arbitrage":
            pmin, pmax = float(prices.min()), float(prices.max())
            pstd = float(prices.std()) if len(prices) > 1 else 0
            has_arb_zone = pmin < 0.35 or pmax > 0.65
            if max_spread <= 0.05 and pstd >= arb_min_std and has_arb_zone:
                results.append((slug, data))
                logger.debug(f"Arb eligible: {slug[:50]} (std={pstd:.3f}, range=[{pmin:.2f},{pmax:.2f}])")
        else:
            results.append((slug, data))

    return results
