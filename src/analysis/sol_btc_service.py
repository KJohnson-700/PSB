"""
SOL-BTC Correlation Service

Tracks SOL (Solana) price alongside BTC to detect lag/correlation opportunities.

Core thesis: when BTC has a strong move (especially a spike), SOL tends to follow
with a lag. We capitalize on this by detecting BTC moves early and positioning in
SOL before the lag catches up.

Technical Indicators (SOL):
- MACD (12/26/9 EMA) on 15m and 5m charts
- RSI (14-period)
- EMA crossovers (9/21/50)
- ATR(14) for volatility

BTC-SOL Correlation:
- Rolling 1h correlation coefficient
- BTC spike detector (defaults: >0.3% in 5min, >0.8% in 15min; configurable per strategy)
- SOL lag measurement since BTC spike
- Lag opportunity flag when SOL hasn't caught up

Multi-Timeframe Trend (SOL):
- 1H trend (macro direction) — EMA crossover + RSI
- 15m trend (trend confirmation) — MACD
- 5m trend (entry timing) — MACD crossover
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Chainlink reference feeds
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"  # Polygon
CHAINLINK_ETH_USD = "0xF9680D99D6C9589e2a93a78A04A279e509205945"  # Polygon
CHAINLINK_SOL_USD = "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC"  # Polygon
CHAINLINK_XRP_USD = "0x785ba89291f676b5386652eB12b30cF361020694"  # Polygon
CHAINLINK_HYPE_USD = "0xf9ce4fE2F0EcE0362cb416844AE179a49591D567"  # Arbitrum
CHAINLINK_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ORACLE_FEEDS = {
    "BTCUSDT": ("polygon", CHAINLINK_BTC_USD),
    "ETHUSDT": ("polygon", CHAINLINK_ETH_USD),
    "SOLUSDT": ("polygon", CHAINLINK_SOL_USD),
    "XRPUSDT": ("polygon", CHAINLINK_XRP_USD),
    "HYPEUSDT": ("arbitrum", CHAINLINK_HYPE_USD),
}


# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class MACDResult:
    """MACD indicator output."""
    macd_line: float = 0.0        # Fast EMA - Slow EMA
    signal_line: float = 0.0      # EMA of MACD
    histogram: float = 0.0        # MACD - Signal
    prev_histogram: float = 0.0   # Previous bar histogram
    crossover: str = "NONE"       # "BULLISH_CROSS", "BEARISH_CROSS", "NONE"
    histogram_rising: bool = False
    above_zero: bool = False


@dataclass
class SOLAnalysis:
    """SOL price data and technical indicators."""
    current_price: float = 0.0
    # EMAs
    ema_9: float = 0.0
    ema_21: float = 0.0
    ema_50: float = 0.0
    # RSI
    rsi_14: float = 50.0
    # MACD — 1H for HTF histogram gate (matches backtest engine's htf_key="1h" for SOL)
    macd_1h: MACDResult = field(default_factory=MACDResult)
    # MACD — 15m for trend confirmation
    macd_15m: MACDResult = field(default_factory=MACDResult)
    # MACD — 5m for entry timing
    macd_5m: MACDResult = field(default_factory=MACDResult)
    # ATR for volatility
    atr_14: float = 0.0
    # Chainlink / oracle verification for the alt leg
    chainlink_price: Optional[float] = None
    chainlink_updated_at: Optional[datetime] = None
    chainlink_network: Optional[str] = None
    oracle_basis_bps: Optional[float] = None
    # Trend
    trend_direction: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    trend_strength: float = 0.0       # 0.0 - 1.0
    # Meta
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class BTCSOLCorrelation:
    """BTC-SOL correlation and lag opportunity metrics."""
    # Rolling correlation
    correlation_1h: float = 0.0       # Pearson correlation over 1h of returns
    # BTC move detection
    btc_move_5m_pct: float = 0.0      # BTC % move in last 5 minutes
    btc_move_15m_pct: float = 0.0     # BTC % move in last 15 minutes
    btc_spike_detected: bool = False  # True if BTC moved >0.3% in 5m or >0.8% in 15m
    btc_spike_direction: str = "NONE" # "UP", "DOWN", "NONE"
    # SOL lag measurement
    sol_move_5m_pct: float = 0.0      # SOL % move in same 5-minute window
    sol_move_15m_pct: float = 0.0     # SOL % move in same 15-minute window
    sol_lag_pct: float = 0.0          # How much SOL is lagging BTC's move (%)
    # Opportunity
    lag_opportunity: bool = False     # True if BTC spiked but SOL hasn't caught up
    opportunity_direction: str = "NONE"  # "LONG" or "SHORT" or "NONE"
    opportunity_magnitude: float = 0.0   # Expected SOL catch-up magnitude (%)
    lag_detected_at: Optional[float] = None  # time.time() when lag was first detected
    # SOL trend (from correlation analysis)
    sol_trend: str = "NEUTRAL"       # "BULLISH", "BEARISH", "NEUTRAL" from SOL's own 1H bias
    # BTC absolute dollar move (for dollar-threshold entry filters)
    btc_move_5m_dollars: float = 0.0   # Absolute BTC $ move in last 5 minutes
    btc_move_15m_dollars: float = 0.0  # Absolute BTC $ move in last 15 minutes
    # BTC reference prices
    btc_price: float = 0.0
    btc_chainlink_price: Optional[float] = None
    btc_chainlink_updated_at: Optional[datetime] = None


@dataclass
class MultiTimeframeTrend:
    """Multi-timeframe trend summary for SOL."""
    # Daily trend — dominant bias (new: gates macro direction)
    daily_trend: str = "NEUTRAL"      # BULLISH, BEARISH, NEUTRAL
    daily_basis: str = ""             # e.g. "price>EMA50, RSI=58"
    # 1H trend — macro direction
    h1_trend: str = "NEUTRAL"         # BULLISH, BEARISH, NEUTRAL
    h1_basis: str = ""                # e.g. "EMA9>EMA21>EMA50, RSI=62"
    # 15m trend — trend confirmation
    m15_trend: str = "NEUTRAL"
    m15_basis: str = ""               # e.g. "MACD above zero, histogram rising"
    # 5m trend — entry timing
    m5_trend: str = "NEUTRAL"
    m5_basis: str = ""                # e.g. "MACD bullish cross"
    # Overall
    aligned: bool = False             # True if at least 3 non-neutral timeframes agree
    overall_direction: str = "NEUTRAL"


@dataclass
class SOLTechnicalAnalysis:
    """Complete SOL technical analysis with BTC correlation."""
    sol: SOLAnalysis = field(default_factory=SOLAnalysis)
    correlation: BTCSOLCorrelation = field(default_factory=BTCSOLCorrelation)
    multi_tf: MultiTimeframeTrend = field(default_factory=MultiTimeframeTrend)
    timestamp: datetime = field(default_factory=datetime.now)


# ══════════════════════════════════════════════════════════════════════
# Main Service
# ══════════════════════════════════════════════════════════════════════

class SOLBTCService:
    """Tracks SOL price alongside BTC for lag/correlation opportunities."""

    BINANCE_HOSTS = [
        "https://api.binance.com",
        "https://api.binance.us",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
    ]

    # Free Polygon RPCs — try multiple
    POLYGON_RPCS = [
        "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic",
        "https://rpc.ankr.com/polygon",
        "https://polygon.llamarpc.com",
    ]
    ARBITRUM_RPCS = [
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arb1.arbitrum.io/rpc",
        "https://rpc.ankr.com/arbitrum",
        "https://arbitrum.llamarpc.com",
    ]

    def __init__(
        self,
        polygon_rpc: str = None,
        alt_symbol: str = "SOLUSDT",
        *,
        dynamic_beta_min: float = 0.8,
        dynamic_beta_max: float = 3.0,
        dynamic_beta_extreme_max: float = 5.0,
        btc_spike_floor_pct_5m: float = 0.3,
        btc_spike_floor_pct_15m: float = 0.8,
        lag_signal_min_pct: float = 0.2,
    ):
        self.polygon_rpc = polygon_rpc
        self.polygon_rpcs = self.POLYGON_RPCS if polygon_rpc is None else [polygon_rpc]
        self.alt_symbol = alt_symbol
        self.dynamic_beta_min = float(dynamic_beta_min)
        self.dynamic_beta_max = float(dynamic_beta_max)
        self.dynamic_beta_extreme_max = float(dynamic_beta_extreme_max)
        self.btc_spike_floor_pct_5m = float(btc_spike_floor_pct_5m)
        self.btc_spike_floor_pct_15m = float(btc_spike_floor_pct_15m)
        self.lag_signal_min_pct = float(lag_signal_min_pct)
        self.spike_z_threshold = 1.5  # Z-score threshold for adaptive BTC spike detection
        self._oracle_clients: Dict[Tuple[str, str], Tuple[object, object]] = {}
        self._cache: Dict[str, Tuple[float, pd.DataFrame]] = {}  # key -> (timestamp, df)
        self._cache_ttl = 60  # seconds
        # (direction, spike_window) -> (first_detected_at, btc_move_abs_pct)
        self._lag_opportunity_state: Dict[Tuple[str, str], Tuple[float, float]] = {}

    # ──────────────────────────────────────────────────────────────────
    # Binance API
    # ──────────────────────────────────────────────────────────────────

    def fetch_klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
        """Fetch klines from Binance for any symbol (SOLUSDT, BTCUSDT, etc.)."""
        cache_key = f"binance_{symbol}_{interval}_{limit}"
        if cache_key in self._cache:
            ts, df = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return df

        last_exc = None
        for host in self.BINANCE_HOSTS:
            try:
                resp = requests.get(
                    f"{host}/api/v3/klines",
                    params={"symbol": symbol, "interval": interval, "limit": limit},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()

                df = pd.DataFrame(data, columns=[
                    "open_time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades",
                    "taker_buy_base", "taker_buy_quote", "ignore",
                ])
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)
                df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
                df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

                self._cache[cache_key] = (time.time(), df)
                return df

            except Exception as e:
                logger.debug(f"Binance host {host} failed for klines ({symbol} {interval}): {e}, trying next...")
                last_exc = e

        logger.error(f"SOL/BTC klines unavailable - all Binance endpoints failed ({symbol} {interval}): {last_exc}")
        return pd.DataFrame()

    def get_current_price(self, symbol: str = "SOLUSDT") -> Optional[float]:
        """Get current price from Binance for any symbol."""
        for host in self.BINANCE_HOSTS:
            try:
                resp = requests.get(
                    f"{host}/api/v3/ticker/price",
                    params={"symbol": symbol},
                    timeout=5,
                )
                resp.raise_for_status()
                return float(resp.json()["price"])
            except Exception as e:
                logger.debug(f"Binance host {host} failed for price ({symbol}): {e}, trying next...")

        logger.error(f"Price unavailable for {symbol} - all Binance endpoints failed. BTC/SOL strategies will skip this cycle.")
        return None

    # ──────────────────────────────────────────────────────────────────
    # Chainlink Oracle Reference Feeds
    # ──────────────────────────────────────────────────────────────────

    def _chainlink_rpcs_for_network(self, network: str) -> List[str]:
        if network == "polygon":
            return list(self.polygon_rpcs)
        if network == "arbitrum":
            return list(self.ARBITRUM_RPCS)
        return []

    def get_chainlink_price_for_symbol(
        self,
        symbol: str,
    ) -> Tuple[Optional[float], Optional[datetime], Optional[str]]:
        """Read asset/USD price from the configured Chainlink reference feed."""
        from web3 import Web3

        feed = ORACLE_FEEDS.get((symbol or "").upper())
        if not feed:
            return None, None, None
        network, address = feed
        cache_key = (network, address)
        cached = self._oracle_clients.get(cache_key)
        if cached is not None:
            try:
                _w3, contract = cached
                round_data = contract.functions.latestRoundData().call()
                _, answer, _, updated_at, _ = round_data
                decimals = contract.functions.decimals().call()
                price = answer / (10 ** decimals)
                updated = datetime.utcfromtimestamp(updated_at)
                return price, updated, network
            except Exception:
                self._oracle_clients.pop(cache_key, None)

        for rpc_url in self._chainlink_rpcs_for_network(network):
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(address),
                    abi=CHAINLINK_ABI,
                )
                round_data = contract.functions.latestRoundData().call()
                _, answer, _, updated_at, _ = round_data
                decimals = contract.functions.decimals().call()
                price = answer / (10 ** decimals)
                updated = datetime.utcfromtimestamp(updated_at)
                self._oracle_clients[cache_key] = (w3, contract)
                logger.info(
                    f"Chainlink {symbol.replace('USDT', '')}/USD: ${price:,.2f} via {network}:{rpc_url}"
                )
                return price, updated, network
            except Exception as e:
                logger.debug(f"Chainlink RPC {network}:{rpc_url} failed for {symbol}: {e}")
                continue

        logger.warning(f"Chainlink: all {network} RPCs failed for {symbol}")
        return None, None, network

    def get_chainlink_btc_price(self) -> Tuple[Optional[float], Optional[datetime]]:
        """Read BTC/USD price from Chainlink reference feed."""
        price, updated, _network = self.get_chainlink_price_for_symbol("BTCUSDT")
        return price, updated

    # ──────────────────────────────────────────────────────────────────
    # Basic Indicators
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(span=period, adjust=False).mean()
        avg_loss = loss.ewm(span=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    # ──────────────────────────────────────────────────────────────────
    # MACD (12/26/9 EMA)
    # ──────────────────────────────────────────────────────────────────

    def calc_macd(self, df: pd.DataFrame,
                  fast: int = 12, slow: int = 26, signal: int = 9) -> MACDResult:
        """Calculate MACD with histogram and crossover detection.

        Settings: fast=12, slow=26, signal=9 (all EMA).
        """
        if len(df) < slow + signal:
            return MACDResult()

        close = df["close"]
        ema_fast = self._calc_ema(close, fast)
        ema_slow = self._calc_ema(close, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self._calc_ema(macd_line, signal)
        histogram = macd_line - signal_line

        curr_macd = float(macd_line.iloc[-1])
        curr_signal = float(signal_line.iloc[-1])
        curr_hist = float(histogram.iloc[-1])
        prev_hist = float(histogram.iloc[-2]) if len(histogram) > 1 else 0.0

        # Crossover detection
        crossover = "NONE"
        if len(macd_line) >= 2:
            prev_macd = float(macd_line.iloc[-2])
            prev_signal = float(signal_line.iloc[-2])
            # Bullish: MACD crosses above signal
            if prev_macd <= prev_signal and curr_macd > curr_signal:
                crossover = "BULLISH_CROSS"
            # Bearish: MACD crosses below signal
            elif prev_macd >= prev_signal and curr_macd < curr_signal:
                crossover = "BEARISH_CROSS"

        return MACDResult(
            macd_line=curr_macd,
            signal_line=curr_signal,
            histogram=curr_hist,
            prev_histogram=prev_hist,
            crossover=crossover,
            histogram_rising=curr_hist > prev_hist,
            above_zero=curr_macd > 0,
        )

    # ──────────────────────────────────────────────────────────────────
    # SOL Indicators (EMAs, RSI, ATR, MACD)
    # ──────────────────────────────────────────────────────────────────

    def calc_sol_indicators(self) -> SOLAnalysis:
        """Calculate all SOL technical indicators.

        Uses 15m data for EMAs/RSI/ATR, plus MACD on 1H (HTF gate), 15m, and 5m.
        """
        df_15m = self.fetch_klines(self.alt_symbol, "15m", 200)
        df_5m = self.fetch_klines(self.alt_symbol, "5m", 100)
        df_1h = self.fetch_klines(self.alt_symbol, "1h", 100)

        if df_15m.empty:
            logger.warning("Could not fetch SOL 15m klines")
            return SOLAnalysis()

        current_price = float(df_15m["close"].iloc[-1])
        close_15m = df_15m["close"]

        # EMAs from 15m
        ema_9 = float(self._calc_ema(close_15m, 9).iloc[-1])
        ema_21 = float(self._calc_ema(close_15m, 21).iloc[-1])
        ema_50 = float(self._calc_ema(close_15m, 50).iloc[-1]) if len(df_15m) >= 50 else 0.0

        # RSI from 15m
        rsi_14 = float(self._calc_rsi(close_15m, 14).iloc[-1])

        # ATR from 15m
        atr_14 = float(self._calc_atr(df_15m, 14).iloc[-1])

        # MACD on 1H (HTF histogram gate — matches backtest engine htf_key="1h" for SOL)
        macd_1h = self.calc_macd(df_1h, fast=12, slow=26, signal=9) if not df_1h.empty and len(df_1h) >= 30 else MACDResult()

        # MACD on 15m (trend confirmation)
        macd_15m = self.calc_macd(df_15m, fast=12, slow=26, signal=9)

        # MACD on 5m (entry timing)
        macd_5m = self.calc_macd(df_5m, fast=12, slow=26, signal=9) if not df_5m.empty else MACDResult()

        # Determine trend from EMA alignment + RSI
        trend_dir, trend_str = self._determine_sol_trend(
            current_price, ema_9, ema_21, ema_50, rsi_14
        )
        cl_price, cl_updated, cl_network = self.get_chainlink_price_for_symbol(self.alt_symbol)
        basis_bps = None
        if cl_price and cl_price > 0:
            basis_bps = ((current_price - cl_price) / cl_price) * 10000.0

        return SOLAnalysis(
            current_price=current_price,
            ema_9=ema_9,
            ema_21=ema_21,
            ema_50=ema_50,
            rsi_14=rsi_14,
            macd_1h=macd_1h,
            macd_15m=macd_15m,
            macd_5m=macd_5m,
            atr_14=atr_14,
            chainlink_price=cl_price,
            chainlink_updated_at=cl_updated,
            chainlink_network=cl_network,
            oracle_basis_bps=basis_bps,
            trend_direction=trend_dir,
            trend_strength=trend_str,
        )

    @staticmethod
    def _determine_sol_trend(
        price: float, ema_9: float, ema_21: float, ema_50: float, rsi: float
    ) -> Tuple[str, float]:
        """Determine SOL trend from EMA alignment and RSI."""
        if ema_50 == 0.0:
            # Not enough data for full analysis
            if price > ema_9 > ema_21:
                return "BULLISH", 0.5
            elif price < ema_9 < ema_21:
                return "BEARISH", 0.5
            return "NEUTRAL", 0.0

        bullish_score = 0
        if price > ema_9:
            bullish_score += 1
        if ema_9 > ema_21:
            bullish_score += 1
        if ema_21 > ema_50:
            bullish_score += 1
        # RSI confirmation
        if rsi > 55:
            bullish_score += 1
        elif rsi < 45:
            bullish_score -= 1

        if bullish_score >= 3:
            return "BULLISH", bullish_score / 4.0
        elif bullish_score <= -1 or (bullish_score == 0 and rsi < 45):
            bearish_count = sum([price < ema_9, ema_9 < ema_21, ema_21 < ema_50])
            if rsi < 45:
                bearish_count += 1
            return "BEARISH", bearish_count / 4.0
        else:
            return "NEUTRAL", 0.3

    # ──────────────────────────────────────────────────────────────────
    # BTC-SOL Correlation & Lag Detection
    # ──────────────────────────────────────────────────────────────────

    def _apply_lag_staleness(
        self,
        result: BTCSOLCorrelation,
        *,
        spike_window: str,
        btc_move_pct: float,
    ) -> None:
        """Expire lag opportunities unless the BTC impulse materially refreshes."""
        if not result.lag_opportunity:
            self._lag_opportunity_state.clear()
            return

        now = time.time()
        key = (result.opportunity_direction, spike_window)
        btc_move_abs = abs(btc_move_pct)
        previous = self._lag_opportunity_state.get(key)

        if previous is None or btc_move_abs > previous[1] + 0.10:
            self._lag_opportunity_state[key] = (now, btc_move_abs)
            previous = self._lag_opportunity_state[key]

        result.lag_detected_at = previous[0]
        lag_age_sec = now - previous[0]
        if lag_age_sec <= 300:
            return

        logger.debug(
            f"Lag opportunity expired: age={lag_age_sec:.0f}s > 300s, "
            f"dir={result.opportunity_direction} mag={result.opportunity_magnitude:.2f}%"
        )
        result.lag_opportunity = False
        result.opportunity_direction = "NONE"
        result.opportunity_magnitude = 0.0

    def calc_correlation(self) -> BTCSOLCorrelation:
        """Calculate BTC-SOL correlation and detect lag opportunities.

        1. Rolling 1h correlation of returns
        2. BTC spike detection (percent floors + z-score on rolling 1m-derived moves)
        3. SOL lag measurement — how much SOL has moved since BTC spike
        4. Lag opportunity — if BTC spiked but SOL hasn't caught up
        """
        result = BTCSOLCorrelation()

        # Fetch 1-minute data for both (last 60 = 1 hour for correlation)
        df_sol_1m = self.fetch_klines(self.alt_symbol, "1m", 60)
        df_btc_1m = self.fetch_klines("BTCUSDT", "1m", 60)

        if df_sol_1m.empty or df_btc_1m.empty:
            logger.warning("Could not fetch 1m klines for correlation")
            return result

        # Current prices
        sol_price = float(df_sol_1m["close"].iloc[-1])
        btc_price = float(df_btc_1m["close"].iloc[-1])
        result.btc_price = btc_price

        # --- Rolling 1h correlation of returns ---
        sol_returns = df_sol_1m["close"].pct_change().dropna()
        btc_returns = df_btc_1m["close"].pct_change().dropna()

        # Align lengths
        min_len = min(len(sol_returns), len(btc_returns))
        if min_len >= 10:
            sol_ret = sol_returns.iloc[-min_len:].values
            btc_ret = btc_returns.iloc[-min_len:].values
            # Pearson correlation
            if np.std(sol_ret) > 0 and np.std(btc_ret) > 0:
                correlation = float(np.corrcoef(sol_ret, btc_ret)[0, 1])
                result.correlation_1h = correlation

        # --- BTC move detection ---
        # 5-minute move: compare current close to close 5 bars ago
        if len(df_btc_1m) >= 6:
            btc_5m_ago = float(df_btc_1m["close"].iloc[-6])
            btc_move_5m = (btc_price - btc_5m_ago) / btc_5m_ago * 100
            result.btc_move_5m_pct = btc_move_5m
            result.btc_move_5m_dollars = abs(btc_price - btc_5m_ago)

        # 15-minute move: compare current close to close 15 bars ago
        if len(df_btc_1m) >= 16:
            btc_15m_ago = float(df_btc_1m["close"].iloc[-16])
            btc_move_15m = (btc_price - btc_15m_ago) / btc_15m_ago * 100
            result.btc_move_15m_pct = btc_move_15m
            result.btc_move_15m_dollars = abs(btc_price - btc_15m_ago)

        # Z-score adaptive spike detection (rolling window on 1m-derived % moves)
        _z_threshold = self.spike_z_threshold
        _closes = df_btc_1m["close"].values
        _moves_5m = [
            abs((_closes[i] - _closes[i - 5]) / _closes[i - 5] * 100)
            for i in range(5, len(_closes))
        ]
        _moves_15m = [
            abs((_closes[i] - _closes[i - 15]) / _closes[i - 15] * 100)
            for i in range(15, len(_closes))
        ]
        _window_5m = _moves_5m[-21:-1] if len(_moves_5m) >= 21 else _moves_5m[:-1]
        _window_15m = _moves_15m[-21:-1] if len(_moves_15m) >= 21 else _moves_15m[:-1]

        spike_5m = False
        spike_15m = False
        _floor5 = self.btc_spike_floor_pct_5m
        _floor15 = self.btc_spike_floor_pct_15m
        if len(_window_5m) >= 5:
            _mean_5m = float(np.mean(_window_5m))
            _std_5m = float(np.std(_window_5m))
            _current_5m = abs(result.btc_move_5m_pct)
            spike_5m = (
                (_current_5m - _mean_5m) / _std_5m > _z_threshold if _std_5m > 0.01
                else (_current_5m > _floor5)
            )
        else:
            spike_5m = abs(result.btc_move_5m_pct) > _floor5

        if len(_window_15m) >= 5:
            _mean_15m = float(np.mean(_window_15m))
            _std_15m = float(np.std(_window_15m))
            _current_15m = abs(result.btc_move_15m_pct)
            spike_15m = (
                (_current_15m - _mean_15m) / _std_15m > _z_threshold
                if _std_15m > 0.01
                else (_current_15m > _floor15)
            )
        else:
            spike_15m = abs(result.btc_move_15m_pct) > _floor15

        if spike_5m or spike_15m:
            result.btc_spike_detected = True
            # Use the stronger signal for direction
            dominant_move = result.btc_move_5m_pct if spike_5m else result.btc_move_15m_pct
            result.btc_spike_direction = "UP" if dominant_move > 0 else "DOWN"

        # --- SOL lag measurement ---
        if len(df_sol_1m) >= 6:
            sol_5m_ago = float(df_sol_1m["close"].iloc[-6])
            sol_move_5m = (sol_price - sol_5m_ago) / sol_5m_ago * 100
            result.sol_move_5m_pct = sol_move_5m

        if len(df_sol_1m) >= 16:
            sol_15m_ago = float(df_sol_1m["close"].iloc[-16])
            sol_move_15m = (sol_price - sol_15m_ago) / sol_15m_ago * 100
            result.sol_move_15m_pct = sol_move_15m

        # --- Lag opportunity detection ---
        # If BTC spiked, check if SOL has caught up
        # SOL typically moves ~1.5-2x BTC's percentage move (academic avg for high-beta alts)
        if result.btc_spike_detected:
            # Dynamic beta: use ratio of SOL vs BTC volatility as a proxy
            # When both have enough data, compare recent move magnitudes
            if len(df_sol_1m) >= 30 and len(df_btc_1m) >= 30:
                sol_returns = df_sol_1m["close"].pct_change().dropna().tail(30)
                btc_returns = df_btc_1m["close"].pct_change().dropna().tail(30)
                btc_var = btc_returns.var()
                if btc_var > 0:
                    dynamic_beta = float(sol_returns.cov(btc_returns) / btc_var)
                    # Regime-aware beta clamp: during extreme BTC spikes (>1.5%),
                    # SOL beta can exceed 4x. Clamping to 3.0 in those regimes
                    # underestimates expected SOL move -> false lag opportunities.
                    btc_spike_pct = max(abs(result.btc_move_5m_pct), abs(result.btc_move_15m_pct))
                    if btc_spike_pct > 1.5:
                        dynamic_beta = max(self.dynamic_beta_min, min(self.dynamic_beta_extreme_max, dynamic_beta))
                    else:
                        dynamic_beta = max(self.dynamic_beta_min, min(self.dynamic_beta_max, dynamic_beta))
                else:
                    dynamic_beta = 1.5
            else:
                dynamic_beta = 1.5

            spike_window = "5m" if spike_5m else "15m"
            btc_move_for_lag = result.btc_move_5m_pct if spike_5m else result.btc_move_15m_pct
            expected_sol_move = btc_move_for_lag * dynamic_beta
            actual_sol_move = result.sol_move_5m_pct if spike_5m else result.sol_move_15m_pct

            # Lag = how much SOL should have moved but hasn't
            if result.btc_spike_direction == "UP":
                lag = expected_sol_move - actual_sol_move
                result.sol_lag_pct = lag
                # Opportunity if SOL is lagging by more than lag_signal_min_pct
                if lag > self.lag_signal_min_pct:
                    result.lag_opportunity = True
                    result.opportunity_direction = "LONG"
                    result.opportunity_magnitude = lag
            elif result.btc_spike_direction == "DOWN":
                lag = actual_sol_move - expected_sol_move  # expected is negative, actual should be too
                result.sol_lag_pct = lag
                # Opportunity if SOL hasn't dropped as much as expected
                if lag > self.lag_signal_min_pct:
                    result.lag_opportunity = True
                    result.opportunity_direction = "SHORT"
                    result.opportunity_magnitude = lag

            self._apply_lag_staleness(
                result,
                spike_window=spike_window,
                btc_move_pct=btc_move_for_lag,
            )
        else:
            self._apply_lag_staleness(result, spike_window="none", btc_move_pct=0.0)

        # --- Chainlink BTC verification ---
        cl_price, cl_updated = self.get_chainlink_btc_price()
        result.btc_chainlink_price = cl_price
        result.btc_chainlink_updated_at = cl_updated

        return result

    # ──────────────────────────────────────────────────────────────────
    # Multi-Timeframe Trend (SOL)
    # ──────────────────────────────────────────────────────────────────

    def calc_multi_tf_trend(self) -> MultiTimeframeTrend:
        """Calculate multi-timeframe trend for SOL.

        - 1H: macro direction using EMA crossover + RSI
        - 15m: trend confirmation using MACD
        - 5m: entry timing using MACD crossover
        """
        result = MultiTimeframeTrend()

        # --- Daily trend (dominant bias — gates everything) ---
        df_1d = self.fetch_klines(self.alt_symbol, "1d", 60)
        if not df_1d.empty and len(df_1d) >= 50:
            close_1d = df_1d["close"]
            price_d = float(close_1d.iloc[-1])
            ema_21d = float(self._calc_ema(close_1d, 21).iloc[-1])
            ema_50d = float(self._calc_ema(close_1d, 50).iloc[-1])
            rsi_d = float(self._calc_rsi(close_1d, 14).iloc[-1])

            # Simple daily bias: price position relative to EMAs + RSI
            d_bull = 0
            if price_d > ema_21d:
                d_bull += 1
            if price_d > ema_50d:
                d_bull += 1
            if ema_21d > ema_50d:
                d_bull += 1
            if rsi_d > 55:
                d_bull += 1
            elif rsi_d < 45:
                d_bull -= 1

            if d_bull >= 3:
                result.daily_trend = "BULLISH"
            elif d_bull <= 0:
                result.daily_trend = "BEARISH"
            else:
                result.daily_trend = "NEUTRAL"

            result.daily_basis = (
                f"price={price_d:.2f} EMA21={ema_21d:.2f} EMA50={ema_50d:.2f} RSI={rsi_d:.0f}"
            )
        elif not df_1d.empty and len(df_1d) >= 21:
            # Fallback: not enough for EMA50, use EMA21 + RSI
            close_1d = df_1d["close"]
            price_d = float(close_1d.iloc[-1])
            ema_21d = float(self._calc_ema(close_1d, 21).iloc[-1])
            rsi_d = float(self._calc_rsi(close_1d, 14).iloc[-1])
            if price_d > ema_21d and rsi_d > 50:
                result.daily_trend = "BULLISH"
            elif price_d < ema_21d and rsi_d < 50:
                result.daily_trend = "BEARISH"
            else:
                result.daily_trend = "NEUTRAL"
            result.daily_basis = f"price={price_d:.2f} EMA21={ema_21d:.2f} RSI={rsi_d:.0f} (no EMA50)"

        # --- 1H trend (macro direction) ---
        df_1h = self.fetch_klines(self.alt_symbol, "1h", 100)
        if not df_1h.empty and len(df_1h) >= 50:
            close_1h = df_1h["close"]
            price = float(close_1h.iloc[-1])
            ema_9 = float(self._calc_ema(close_1h, 9).iloc[-1])
            ema_21 = float(self._calc_ema(close_1h, 21).iloc[-1])
            ema_50 = float(self._calc_ema(close_1h, 50).iloc[-1])
            rsi = float(self._calc_rsi(close_1h, 14).iloc[-1])

            h1_trend, _ = self._determine_sol_trend(price, ema_9, ema_21, ema_50, rsi)
            result.h1_trend = h1_trend
            result.h1_basis = (
                f"EMA9={ema_9:.2f} EMA21={ema_21:.2f} EMA50={ema_50:.2f} RSI={rsi:.0f}"
            )

        # --- 15m trend (confirmation via MACD) ---
        df_15m = self.fetch_klines(self.alt_symbol, "15m", 100)
        if not df_15m.empty:
            macd_15m = self.calc_macd(df_15m, fast=12, slow=26, signal=9)

            if macd_15m.above_zero and macd_15m.histogram_rising:
                result.m15_trend = "BULLISH"
            elif not macd_15m.above_zero and not macd_15m.histogram_rising:
                result.m15_trend = "BEARISH"
            else:
                result.m15_trend = "NEUTRAL"

            result.m15_basis = (
                f"MACD={macd_15m.macd_line:.4f} hist={macd_15m.histogram:.4f} "
                f"{'rising' if macd_15m.histogram_rising else 'falling'} {macd_15m.crossover}"
            )

        # --- 5m trend (entry timing via MACD crossover) ---
        df_5m = self.fetch_klines(self.alt_symbol, "5m", 100)
        if not df_5m.empty:
            macd_5m = self.calc_macd(df_5m, fast=12, slow=26, signal=9)

            if macd_5m.crossover == "BULLISH_CROSS":
                result.m5_trend = "BULLISH"
            elif macd_5m.crossover == "BEARISH_CROSS":
                result.m5_trend = "BEARISH"
            elif macd_5m.above_zero and macd_5m.histogram_rising:
                result.m5_trend = "BULLISH"
            elif not macd_5m.above_zero and not macd_5m.histogram_rising:
                result.m5_trend = "BEARISH"
            else:
                result.m5_trend = "NEUTRAL"

            result.m5_basis = (
                f"MACD={macd_5m.macd_line:.4f} hist={macd_5m.histogram:.4f} "
                f"{'rising' if macd_5m.histogram_rising else 'falling'} {macd_5m.crossover}"
            )

        # --- Overall alignment ---
        trends = [result.daily_trend, result.h1_trend, result.m15_trend, result.m5_trend]
        non_neutral = [t for t in trends if t != "NEUTRAL"]
        result.aligned = len(non_neutral) >= 3 and len(set(non_neutral)) == 1

        # Weighted overall: Daily=4, 1H=3, 15m=2, 5m=1
        scores = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
        scores[result.daily_trend] += 4
        scores[result.h1_trend] += 3
        scores[result.m15_trend] += 2
        scores[result.m5_trend] += 1
        result.overall_direction = max(scores, key=scores.get)

        return result

    # ──────────────────────────────────────────────────────────────────
    # Full Analysis (everything combined)
    # ──────────────────────────────────────────────────────────────────

    def get_full_analysis(self) -> Optional[SOLTechnicalAnalysis]:
        """Perform full SOL-BTC technical analysis.

        Fetches SOL data across timeframes, calculates indicators,
        measures BTC-SOL correlation, and detects lag opportunities.

        Returns SOLTechnicalAnalysis with:
        - SOL indicators (MACD 15m/5m, RSI, EMAs, ATR)
        - BTC-SOL correlation and lag opportunity
        - Multi-timeframe trend (1H/15m/5m)
        """
        try:
            # SOL indicators
            sol = self.calc_sol_indicators()
            if sol.current_price == 0.0:
                logger.warning("Could not fetch SOL price data for analysis")
                return None

            # BTC-SOL correlation
            correlation = self.calc_correlation()

            # Multi-timeframe trend
            multi_tf = self.calc_multi_tf_trend()

            # Populate sol_trend on the correlation object from the multi-TF 1H reading.
            # BTCSOLCorrelation.sol_trend defaults to "NEUTRAL" and calc_correlation()
            # doesn't set it — so the NEUTRAL macro fallback path in sol_macro.py always
            # got sol_trend="NEUTRAL", making allowed_side=None and return [].
            # Use 1H trend as the directional bias for NEUTRAL macro fallback.
            correlation.sol_trend = multi_tf.h1_trend

            analysis = SOLTechnicalAnalysis(
                sol=sol,
                correlation=correlation,
                multi_tf=multi_tf,
            )

            # Log summary
            lag_info = ""
            if correlation.lag_opportunity:
                lag_info = (
                    f" | LAG OPP: {correlation.opportunity_direction} "
                    f"+{correlation.opportunity_magnitude:.2f}%"
                )

            _alt_label = self.alt_symbol.replace("USDT", "")
            logger.info(
                f"{_alt_label} ${sol.current_price:.2f} | {sol.trend_direction} ({sol.trend_strength:.0%}) | "
                f"MACD15m={sol.macd_15m.histogram:+.4f} {sol.macd_15m.crossover} | "
                f"MACD5m={sol.macd_5m.histogram:+.4f} {sol.macd_5m.crossover} | "
                f"RSI={sol.rsi_14:.0f} ATR={sol.atr_14:.3f} | "
                f"BTC ${correlation.btc_price:,.0f} corr={correlation.correlation_1h:.2f} "
                f"BTC5m={correlation.btc_move_5m_pct:+.2f}% {_alt_label}5m={correlation.sol_move_5m_pct:+.2f}% | "
                f"D={multi_tf.daily_trend} MTF={multi_tf.overall_direction} aligned={multi_tf.aligned}"
                f"{f' | oracle={sol.chainlink_network}:${sol.chainlink_price:,.2f} basis={sol.oracle_basis_bps:+.1f}bps' if sol.chainlink_price else ''}"
                f"{lag_info}"
            )

            return analysis

        except Exception as e:
            logger.error(f"SOL-BTC analysis failed: {e}", exc_info=True)
            return None
