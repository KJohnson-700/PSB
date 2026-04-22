"""
Historical data loader for Polymarket backtesting.
Supports: Polymarket CLOB API (free), PolymarketData API (paid), local CSV/Parquet,
          Jon-Becker prediction-market-analysis dataset (36GB, trade-level data).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd
import requests

logger = logging.getLogger(__name__)

POLYMARKETDATA_BASE = "https://api.polymarketdata.co/v1"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketLoader:
    """
    Uses Polymarket's free Gamma + CLOB APIs (no auth).
    Resolves slug -> token ID via Gamma, fetches prices via CLOB /prices-history.
    """

    def __init__(self):
        self.session = requests.Session()

    def _get(self, url: str, params: Dict = None, timeout: int = 30) -> Optional[Dict]:
        try:
            r = self.session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json() if r.content else None
        except requests.RequestException as e:
            logger.error(f"Polymarket API error: {e}")
            return None

    def get_resolution_outcome(self, slug: str) -> Optional[bool]:
        """
        Fetch resolution outcome from Gamma API for resolved markets.
        Returns: True if YES won, False if NO won, None if unknown/unresolved.
        """
        for endpoint, key in [("markets", None), ("events", "markets")]:
            data = self._get(
                f"{GAMMA_BASE}/{endpoint}", params={"slug": slug, "limit": 5}
            )
            if not data:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                markets = item.get(key, []) if key else [item]
                for m in markets:
                    if not isinstance(m, dict):
                        continue
                    if m.get("slug") and m.get("slug") != slug and key == "markets":
                        continue
                    if not m.get("closed"):
                        continue
                    op = m.get("outcomePrices")
                    if not op:
                        continue
                    if isinstance(op, str):
                        try:
                            op = json.loads(op)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(op, (list, tuple)) and len(op) >= 2:
                        yes_final = float(op[0]) if op[0] else 0
                        if yes_final >= 0.99:
                            return True
                        if yes_final <= 0.01:
                            return False
        return None

    def slug_to_token_id(self, slug: str) -> Optional[str]:
        """Resolve event/market slug to YES token ID via Gamma API."""
        for endpoint, key in [("events", "markets"), ("markets", None)]:
            data = self._get(
                f"{GAMMA_BASE}/{endpoint}", params={"slug": slug, "limit": 5}
            )
            if not data:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                markets = item.get(key, []) if key else [item]
                for m in markets:
                    if not isinstance(m, dict):
                        continue
                    ids = m.get("clobTokenIds")
                    if not ids:
                        continue
                    if isinstance(ids, str):
                        try:
                            ids = json.loads(ids)
                        except json.JSONDecodeError:
                            continue
                    if ids:
                        return str(ids[0])
        return None

    def get_market_end_date(self, slug: str) -> Optional[datetime]:
        """Fetch market end_date from Gamma API for a given slug."""
        for endpoint, key in [("events", "markets"), ("markets", None)]:
            data = self._get(
                f"{GAMMA_BASE}/{endpoint}", params={"slug": slug, "limit": 5}
            )
            if not data:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                markets = item.get(key, []) if key else [item]
                for m in markets:
                    if not isinstance(m, dict):
                        continue
                    end_str = m.get("endDate") or m.get("end_date_iso")
                    if end_str:
                        try:
                            return datetime.fromisoformat(
                                end_str.replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                        except (ValueError, TypeError):
                            pass
        return None

    def fetch_prices_history(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        interval: str = "1h",
    ) -> pd.DataFrame:
        """Fetch price history from CLOB API (no auth). Chunks large ranges to avoid 400.

        Known limitation: Polymarket CLOB /prices-history only returns data at 12+ hour
        granularity for resolved/closed markets (data retention policy). If the initial
        request returns empty, this method automatically retries with fidelity=720 (12h).
        """
        all_rows = []
        chunk_days = 15
        chunk_sec = chunk_days * 86400
        ts = start_ts
        # fidelity in minutes: 60=1h, 360=6h, 720=12h
        fidelity_min = {"1h": 60, "6h": 360, "1d": 1440, "1m": 1}.get(interval, 60)

        def _fetch_chunked(fidelity: int) -> list:
            rows = []
            t = start_ts
            while t < end_ts:
                chunk_end = min(t + chunk_sec, end_ts)
                data = self._get(
                    f"{CLOB_BASE}/prices-history",
                    params={
                        "market": token_id,
                        "startTs": t,
                        "endTs": chunk_end,
                        "fidelity": fidelity,
                    },
                )
                if data and "history" in data and data["history"]:
                    rows.extend(data["history"])
                t = chunk_end
            return rows

        all_rows = _fetch_chunked(fidelity_min)

        # Fallback: resolved/closed markets only return data at 12h+ granularity
        if not all_rows and fidelity_min < 720:
            logger.debug(
                f"Empty price history for {token_id} at fidelity={fidelity_min}m, "
                f"retrying at 720m (12h) for resolved market compatibility"
            )
            all_rows = _fetch_chunked(720)
        if not all_rows:
            return pd.DataFrame()
        df = pd.DataFrame(all_rows)
        df = df.rename(columns={"p": "price"})
        ts = df["t"]
        unit = "ms" if ts.max() > 1e12 else "s"
        df["t"] = pd.to_datetime(df["t"], unit=unit, utc=True)
        df["spread"] = 0.02
        return df.set_index("t").sort_index()

    def load_market_data(
        self,
        slug: str,
        start_date: str,
        end_date: str,
        resolution: str = "1h",
    ) -> Optional[pd.DataFrame]:
        """Load historical prices for a market by slug.

        Tries CLOB API first, falls back to Jon-Becker dataset if available.
        """
        token_id = self.slug_to_token_id(slug)
        if not token_id:
            logger.debug(f"Could not resolve slug to token: {slug}")
            return None
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.replace(hour=23, minute=59, second=59).timestamp())
        except ValueError:
            return None
        interval = "1h" if resolution in ("1h", "1m") else resolution
        df = self.fetch_prices_history(token_id, start_ts, end_ts, interval)
        if df.empty:
            return None
        return df


class PolymarketDataLoader:
    """
    Fetches historical prices, metrics, and order books from PolymarketData API.
    Requires POLYMARKETDATA_API_KEY in secrets.env.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("POLYMARKETDATA_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["X-API-Key"] = self.api_key

    def _get(self, url: str, params: Dict = None, timeout: int = 60) -> Optional[Dict]:
        try:
            r = self.session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"PolymarketData API error: {e}")
            return None

    def fetch_markets(self, search: str = "", limit: int = 200) -> pd.DataFrame:
        """Fetch market list (for universe selection)."""
        data = self._get(
            f"{POLYMARKETDATA_BASE}/markets",
            params={"search": search, "limit": limit},
        )
        if not data or "data" not in data:
            return pd.DataFrame()
        return pd.DataFrame(data["data"])

    def fetch_prices(
        self,
        slug: str,
        start_ts: str,
        end_ts: str,
        resolution: str = "1m",
    ) -> pd.DataFrame:
        """Fetch historical prices for a market."""
        data = self._get(
            f"{POLYMARKETDATA_BASE}/markets/{slug}/prices",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "resolution": resolution,
            },
        )
        if not data or "data" not in data:
            return pd.DataFrame()
        df = pd.DataFrame(data["data"])
        if not df.empty:
            df["t"] = pd.to_datetime(df["t"], utc=True)
        return df

    def fetch_metrics(
        self,
        slug: str,
        start_ts: str,
        end_ts: str,
        resolution: str = "1m",
    ) -> pd.DataFrame:
        """Fetch historical metrics (spread, liquidity, etc.)."""
        data = self._get(
            f"{POLYMARKETDATA_BASE}/markets/{slug}/metrics",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "resolution": resolution,
            },
        )
        if not data or "data" not in data:
            return pd.DataFrame()
        df = pd.DataFrame(data["data"])
        if not df.empty:
            df["t"] = pd.to_datetime(df["t"], utc=True)
        return df

    def fetch_books(
        self,
        slug: str,
        start_ts: str,
        end_ts: str,
        resolution: str = "5m",
    ) -> List[Dict]:
        """Fetch L2 order book snapshots for fill simulation."""
        data = self._get(
            f"{POLYMARKETDATA_BASE}/markets/{slug}/books",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "resolution": resolution,
            },
        )
        if not data or "data" not in data:
            return []
        return data["data"]

    def load_market_data(
        self,
        slug: str,
        start_date: str,
        end_date: str,
        resolution: str = "1m",
    ) -> Optional[pd.DataFrame]:
        """
        Load merged prices + metrics for a market.
        Returns DataFrame with columns: t, price, spread, (other metrics).
        """
        start_ts = f"{start_date}T00:00:00Z"
        end_ts = f"{end_date}T23:59:00Z"

        prices_df = self.fetch_prices(slug, start_ts, end_ts, resolution)
        metrics_df = self.fetch_metrics(slug, start_ts, end_ts, resolution)

        if prices_df.empty:
            logger.warning(f"No price data for {slug}")
            return None
        if metrics_df.empty:
            logger.warning(f"No metrics for {slug}, using prices only")
            prices_df = prices_df.rename(columns={"p": "price"})
            prices_df["spread"] = 0.02  # default
            return prices_df.set_index("t").sort_index()

        prices_df = prices_df.rename(columns={"p": "price"})
        merged = (
            prices_df.merge(metrics_df, on="t", how="inner")
            .sort_values("t")
            .set_index("t")
        )
        if len(merged) < len(prices_df) * 0.95:
            logger.warning(
                f"Join dropped >5% rows for {slug}: {len(merged)} vs {len(prices_df)}"
            )
        return merged


class LocalDataLoader:
    """
    Load historical data from local CSV or Parquet files.
    Expected format: columns include 't' (timestamp), 'price', 'spread'.
    """

    def __init__(self, data_dir: str = "data/backtest"):
        self.data_dir = Path(data_dir)

    def load_from_csv(self, filepath: str) -> Optional[pd.DataFrame]:
        """Load from CSV. Expects columns: t, price, spread (or p for price)."""
        path = Path(filepath)
        if not path.exists():
            path = self.data_dir / filepath
        if not path.exists():
            return None
        df = pd.read_csv(path)
        if "t" not in df.columns and "timestamp" in df.columns:
            df["t"] = df["timestamp"]
        if "p" in df.columns and "price" not in df.columns:
            df["price"] = df["p"]
        if "spread" not in df.columns:
            df["spread"] = 0.02
        df["t"] = pd.to_datetime(df["t"], utc=True)
        return df.set_index("t").sort_index()

    def load_from_parquet(self, filepath: str) -> Optional[pd.DataFrame]:
        """Load from Parquet."""
        path = Path(filepath)
        if not path.exists():
            path = self.data_dir / filepath
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        if "t" not in df.columns and "timestamp" in df.columns:
            df["t"] = df["timestamp"]
        if "p" in df.columns and "price" not in df.columns:
            df["price"] = df["p"]
        if "spread" not in df.columns:
            df["spread"] = 0.02
        df["t"] = pd.to_datetime(df["t"], utc=True)
        return df.set_index("t").sort_index()
