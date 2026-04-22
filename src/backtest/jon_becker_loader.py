"""
Jon-Becker Dataset Loader
Reads Parquet files from Jon-Becker/prediction-market-analysis dataset.

Dataset: https://github.com/Jon-Becker/prediction-market-analysis
Download: cd prediction-market-analysis && make setup (36GB from Cloudflare R2)

Data layout expected:
    data_dir/
    ├── polymarket/
    │   ├── markets/          # Parquet files: market metadata
    │   ├── trades/           # Parquet files: individual trade records
    │   └── blocks/           # Parquet files: block_number -> timestamp
    └── ...

Usage:
    loader = JonBeckerDataLoader("path/to/prediction-market-analysis/data")
    df = loader.load_market_data("bitcoin-above-100k-2025", "2024-10-01", "2024-11-30", resolution="1h")
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class JonBeckerDataLoader:
    """Load historical Polymarket data from Jon-Becker dataset (Parquet format).

    This solves the CLOB API's 12h fidelity limit for resolved markets by using
    trade-level data to reconstruct prices at any resolution.

    Dataset provides:
    - markets/: market metadata (slug, question, end_date, outcomes)
    - trades/: individual OrderFilled events (block_number, amounts, fees)
    - blocks/: block_number -> timestamp mapping
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.pm_dir = self.data_dir / "polymarket"
        self.markets_dir = self.pm_dir / "markets"
        self.trades_dir = self.pm_dir / "trades"
        self.blocks_dir = self.pm_dir / "blocks"
        self._blocks_cache: Optional[pd.DataFrame] = None
        self._markets_cache: Optional[pd.DataFrame] = None

    def is_available(self) -> bool:
        """Check if the Jon-Becker dataset is present."""
        return (
            self.markets_dir.exists()
            and self.trades_dir.exists()
            and self.blocks_dir.exists()
        )

    def _load_parquet_dir(self, directory: Path) -> pd.DataFrame:
        """Load all Parquet files from a directory into a single DataFrame."""
        files = sorted(directory.glob("*.parquet"))
        if not files:
            # Also try _0.parquet pattern (Hive partitioning)
            files = sorted(directory.rglob("*.parquet"))
        if not files:
            return pd.DataFrame()
        dfs = []
        for f in files:
            try:
                dfs.append(pd.read_parquet(f))
            except Exception as e:
                logger.debug(f"Failed to read {f}: {e}")
        if not dfs:
            return pd.DataFrame()
        return pd.concat(dfs, ignore_index=True)

    def _get_blocks(self) -> pd.DataFrame:
        """Load and cache block number -> timestamp mapping."""
        if self._blocks_cache is None:
            logger.info("Loading block timestamps from Jon-Becker dataset...")
            self._blocks_cache = self._load_parquet_dir(self.blocks_dir)
            if not self._blocks_cache.empty:
                self._blocks_cache["timestamp"] = pd.to_datetime(
                    self._blocks_cache["timestamp"], utc=True
                )
                logger.info(f"Loaded {len(self._blocks_cache):,} block timestamps")
        return self._blocks_cache

    def _get_markets(self) -> pd.DataFrame:
        """Load and cache market metadata."""
        if self._markets_cache is None:
            logger.info("Loading market metadata from Jon-Becker dataset...")
            self._markets_cache = self._load_parquet_dir(self.markets_dir)
            if not self._markets_cache.empty and "slug" in self._markets_cache.columns:
                self._markets_cache = self._markets_cache.set_index("slug", drop=False)
                logger.info(f"Loaded {len(self._markets_cache):,} market records")
        return self._markets_cache

    def find_market_by_slug(self, slug: str) -> Optional[pd.Series]:
        """Find a market by its slug."""
        markets = self._get_markets()
        if markets.empty or slug not in markets.index:
            return None
        return markets.loc[slug]

    def find_market_by_question(self, question: str) -> Optional[pd.DataFrame]:
        """Find markets matching a question substring."""
        markets = self._get_markets()
        if markets.empty or "question" not in markets.columns:
            return None
        mask = markets["question"].str.contains(question, case=False, na=False)
        return markets[mask]

    def load_market_data(
        self,
        slug: str,
        start_date: str,
        end_date: str,
        resolution: str = "1h",
    ) -> Optional[pd.DataFrame]:
        """Load historical price data for a market, reconstructed from trades.

        Args:
            slug: Market slug (e.g., "bitcoin-above-100k-2025")
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            resolution: Candle resolution (e.g., "1m", "5m", "1h", "1d")

        Returns:
            DataFrame with DatetimeIndex and columns: price, spread, volume
        """
        if not self.is_available():
            logger.warning("Jon-Becker dataset not available")
            return None

        market = self.find_market_by_slug(slug)
        if market is None:
            logger.debug(f"Market not found in Jon-Becker dataset: {slug}")
            return None

        market_id = (
            market["id"] if isinstance(market, pd.Series) else market["id"].iloc[0]
        )

        # Load trades for this market
        trades = self._load_trades_for_market(market_id)
        if trades.empty:
            logger.debug(f"No trades found for market {slug} (id={market_id})")
            return None

        # Join with block timestamps
        blocks = self._get_blocks()
        if blocks.empty:
            logger.warning("No block timestamps available")
            return None

        trades = trades.merge(
            blocks[["block_number", "timestamp"]],
            on="block_number",
            how="left",
        )
        trades = trades.dropna(subset=["timestamp"])
        trades = trades.set_index("timestamp").sort_index()

        # Calculate price from maker/taker amounts
        # Polymarket CTF Exchange: maker_amount is USDC (6 decimals), taker_amount is outcome tokens
        # Price = taker_amount_tokens / maker_amount_usdc (in USDC terms)
        # Or vice versa depending on who is buying/selling
        trades["price"] = trades.apply(self._calculate_trade_price, axis=1)
        trades = trades[trades["price"].between(0.001, 0.999)]

        # Filter to date range
        start_dt = pd.Timestamp(start_date, tz="UTC")
        end_dt = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
        trades = trades.loc[start_dt:end_dt]

        if trades.empty:
            logger.debug(f"No trades in date range for {slug}")
            return None

        # Resample to desired resolution
        freq_map = {
            "1m": "1T",
            "5m": "5T",
            "15m": "15T",
            "1h": "1H",
            "4h": "4H",
            "1d": "1D",
        }
        freq = freq_map.get(resolution, resolution)

        try:
            ohlc = trades["price"].resample(freq).agg(["first", "last", "min", "max"])
            ohlc = ohlc.rename(
                columns={"first": "open", "last": "price", "min": "low", "max": "high"}
            )
            ohlc = ohlc.dropna(subset=["price"])

            # Volume from trade counts
            volume = (
                trades["taker_amount"].resample(freq).sum() / 1e6
            )  # USDC, 6 decimals
            ohlc["volume"] = volume.reindex(ohlc.index).fillna(0)

            # Default spread (no L2 book data in this dataset)
            ohlc["spread"] = 0.02

            return ohlc[["price", "spread", "volume"]]

        except Exception as e:
            logger.error(f"Error resampling trades for {slug}: {e}")
            return None

    def _load_trades_for_market(self, market_id: str) -> pd.DataFrame:
        """Load all trades for a specific market ID from the trades Parquet files."""
        # The trades directory may be large, so we filter during load
        trades = self._load_parquet_dir(self.trades_dir)
        if trades.empty:
            return trades

        # Filter by asset ID — Polymarket uses token IDs as asset identifiers
        # The market_id may be a condition_id or token_id, try matching on relevant columns
        for col in ["taker_asset_id", "maker_asset_id"]:
            if col in trades.columns:
                filtered = trades[trades[col].astype(str) == str(market_id)]
                if not filtered.empty:
                    return filtered

        # Fallback: try matching on order_hash substring
        if "order_hash" in trades.columns:
            filtered = trades[
                trades["order_hash"].astype(str).str.contains(str(market_id)[:8])
            ]
            if not filtered.empty:
                return filtered

        return trades

    def _calculate_trade_price(self, row) -> float:
        """Calculate the effective price from a trade record.

        Polymarket CTF Exchange trades have:
        - maker_amount: what the maker gave (USDC, 6 decimals)
        - taker_amount: what the taker gave (depends on side)
        - maker_asset_id: 0 for USDC, token_id for outcome tokens
        """
        try:
            maker_amount = float(row.get("maker_amount", 0))
            taker_amount = float(row.get("taker_amount", 0))
            maker_asset_id = str(row.get("maker_asset_id", ""))

            if maker_amount <= 0 or taker_amount <= 0:
                return 0.5

            # If maker_asset_id is "0", maker is selling USDC (buying outcome tokens)
            if maker_asset_id == "0":
                # Price = USDC paid / tokens received (in USDC terms)
                # Normalize: maker_amount is in 6 decimals, taker_amount in tokens
                price = maker_amount / taker_amount if taker_amount > 0 else 0.5
                # Prices should be between 0 and 1
                if price > 1:
                    price = taker_amount / maker_amount
            else:
                # Maker is selling tokens (taker is buying with USDC)
                price = taker_amount / maker_amount if maker_amount > 0 else 0.5
                if price > 1:
                    price = maker_amount / taker_amount

            return max(0.001, min(0.999, price))

        except (ValueError, TypeError, ZeroDivisionError):
            return 0.5

    def list_available_slugs(self) -> List[str]:
        """List all market slugs available in the dataset."""
        markets = self._get_markets()
        if markets.empty or "slug" not in markets.columns:
            return []
        return sorted(markets["slug"].dropna().unique().tolist())

    def get_market_resolution(self, slug: str) -> Optional[bool]:
        """Get resolution outcome for a market (Yes=True, No=False, unresolved=None)."""
        market = self.find_market_by_slug(slug)
        if market is None:
            return None

        outcomes = market.get("outcomes", "[]")
        outcome_prices = market.get("outcome_prices", "[]")

        try:
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(outcome_prices, str):
                outcome_prices = [float(p) for p in json.loads(outcome_prices)]
        except (json.JSONDecodeError, ValueError):
            return None

        if market.get("closed") and outcome_prices and len(outcome_prices) >= 2:
            return outcome_prices[0] > outcome_prices[1]

        return None
