"""
Bitcoin Price Service v2

Provides real-time BTC price data from multiple sources:
1. Binance API (primary) — free, no key, 1000 candles per request
2. Chainlink BTC/USD Oracle on Polygon (on-chain verification)

Technical Indicators:
- MACD (12/26/9 EMA) — momentum & crossover signals
- Adaptive Trend Sabre (BOSWaves port) — SMA(35), ATR trailing stop, snap S/R, tension
- EMA crossovers (9/21/50/200)
- RSI (14-period)
- Support/Resistance from swing highs/lows + snap levels
- Multi-timeframe trend analysis

Timing Logic:
- Early-candle momentum detection (first 4 of 15 min / first 1.5 of 5 min)
- Prediction window (9-12 of 15 min / 3-4 of 5 min)
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests
from scipy.signal import argrelextrema

logger = logging.getLogger(__name__)

# Chainlink BTC/USD on Polygon Mainnet
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
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
class TrendSabreResult:
    """Adaptive Trend Sabre (BOSWaves) output."""
    ma_value: float = 0.0         # SMA(35)
    trail_value: float = 0.0      # Trailing stop level
    trend: int = 0                # 1 = bullish, -1 = bearish
    tension: float = 0.0          # (price - MA) / ATR — how stretched price is
    tension_abs: float = 0.0
    atr: float = 0.0              # ATR(14)
    bull_signal: bool = False     # Trend just flipped bullish
    bear_signal: bool = False     # Trend just flipped bearish
    snap_supports: List[float] = field(default_factory=list)    # Dynamic snap S/R
    snap_resistances: List[float] = field(default_factory=list)
    bull_retest: bool = False     # Price retested support and held
    bear_retest: bool = False     # Price retested resistance and rejected


@dataclass
class CandleMomentum:
    """Early-candle momentum detection for 15m and 5m charts."""
    # 15-minute candle
    m15_direction: str = "NONE"      # "SPIKE_UP", "SPIKE_DOWN", "DRIFT_UP", "DRIFT_DOWN", "NONE"
    m15_move_pct: float = 0.0        # % move in first 4 minutes
    m15_in_prediction_window: bool = False   # True if we're in min 9-12
    m15_candle_age_minutes: float = 0.0
    # 5-minute candle
    m5_direction: str = "NONE"       # Same signals, scaled for 5m
    m5_move_pct: float = 0.0         # % move in first 1.5 minutes
    m5_in_prediction_window: bool = False    # True if we're in min 3-4
    m5_candle_age_minutes: float = 0.0
    # Combined signal
    momentum_signal: str = "NEUTRAL"  # "STRONG_UP", "STRONG_DOWN", "LEAN_UP", "LEAN_DOWN", "NEUTRAL"
    momentum_strength: float = 0.0    # 0.0 - 1.0


@dataclass
class AnchoredVolumeProfile:
    """Anchored Volume Profile — volume distribution by price since an anchor point.

    Shows where the most trading occurred (high volume nodes = strong S/R)
    and where gaps exist (low volume nodes = price moves fast through these).
    """
    poc_price: float = 0.0          # Point of Control — price with highest volume
    vah_price: float = 0.0          # Value Area High (70% of volume above this)
    val_price: float = 0.0          # Value Area Low (70% of volume below this)
    high_volume_nodes: List[float] = field(default_factory=list)  # Strong S/R levels
    low_volume_nodes: List[float] = field(default_factory=list)   # Fast-move zones
    anchor_type: str = ""           # What event we anchored to
    anchor_price: float = 0.0
    total_volume: float = 0.0
    num_bins: int = 0


@dataclass
class TechnicalAnalysis:
    """Full technical analysis on BTC price data."""
    current_price: float
    # EMAs
    ema_9: float = 0.0
    ema_21: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    # RSI
    rsi_14: float = 50.0
    # MACD — higher TF (4H) for trend filter
    macd_4h: MACDResult = field(default_factory=MACDResult)
    # MACD — intermediate TF (1H) for fallback gate when 4H is decelerating
    macd_1h: MACDResult = field(default_factory=MACDResult)
    # MACD — lower TF (15m) for entry confirmation
    macd_15m: MACDResult = field(default_factory=MACDResult)
    # Adaptive Trend Sabre (4H)
    trend_sabre: TrendSabreResult = field(default_factory=TrendSabreResult)
    # Candle Momentum
    candle_momentum: CandleMomentum = field(default_factory=CandleMomentum)
    # Trend
    trend_direction: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    trend_strength: float = 0.0       # 0.0 - 1.0
    # Support/Resistance (merged: swing + snap levels)
    nearest_support: float = 0.0
    nearest_resistance: float = 0.0
    support_levels: List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    # Multi-timeframe
    daily_trend: str = "NEUTRAL"
    h4_trend: str = "NEUTRAL"
    h1_trend: str = "NEUTRAL"
    # Anchored Volume Profile
    volume_profile: AnchoredVolumeProfile = field(default_factory=AnchoredVolumeProfile)
    # Chainlink
    chainlink_price: Optional[float] = None
    chainlink_updated_at: Optional[datetime] = None
    # Meta
    timestamp: datetime = field(default_factory=datetime.now)


# ══════════════════════════════════════════════════════════════════════
# Main Service
# ══════════════════════════════════════════════════════════════════════

class BTCPriceService:
    """Fetches BTC price data and computes technical indicators."""

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

    def __init__(self, polygon_rpc: str = None):
        self.polygon_rpc = polygon_rpc
        self.polygon_rpcs = self.POLYGON_RPCS if polygon_rpc is None else [polygon_rpc]
        self._w3 = None
        self._chainlink_contract = None
        self._cache: Dict[str, Tuple[float, pd.DataFrame]] = {}  # key -> (timestamp, df)
        self._cache_ttl = 60  # seconds

    # ──────────────────────────────────────────────────────────────────
    # Binance API
    # ──────────────────────────────────────────────────────────────────

    def fetch_klines(self, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
        """Fetch BTC/USDT klines from Binance."""
        cache_key = f"binance_{interval}_{limit}"
        if cache_key in self._cache:
            ts, df = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return df

        last_exc = None
        for host in self.BINANCE_HOSTS:
            try:
                resp = requests.get(
                    f"{host}/api/v3/klines",
                    params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
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
                logger.debug(f"Binance host {host} failed for klines ({interval}): {e}, trying next...")
                last_exc = e

        logger.error(f"BTC klines unavailable - all Binance endpoints failed ({interval}): {last_exc}")
        return pd.DataFrame()

    def get_current_price(self) -> Optional[float]:
        """Get current BTC/USDT price from Binance."""
        for host in self.BINANCE_HOSTS:
            try:
                resp = requests.get(
                    f"{host}/api/v3/ticker/price",
                    params={"symbol": "BTCUSDT"},
                    timeout=5,
                )
                resp.raise_for_status()
                return float(resp.json()["price"])
            except Exception as e:
                logger.debug(f"Binance host {host} failed for price: {e}, trying next...")

        logger.error("BTC price unavailable - all Binance endpoints failed. BTC/SOL strategies will skip this cycle.")
        return None

    # ──────────────────────────────────────────────────────────────────
    # Chainlink Oracle (Polygon)
    # ──────────────────────────────────────────────────────────────────

    def get_chainlink_price(self) -> Tuple[Optional[float], Optional[datetime]]:
        """Read BTC/USD price from Chainlink oracle on Polygon."""
        from web3 import Web3

        if self._w3 is not None and self._chainlink_contract is not None:
            try:
                round_data = self._chainlink_contract.functions.latestRoundData().call()
                _, answer, _, updated_at, _ = round_data
                decimals = self._chainlink_contract.functions.decimals().call()
                price = answer / (10 ** decimals)
                updated = datetime.utcfromtimestamp(updated_at)
                return price, updated
            except Exception:
                self._w3 = None
                self._chainlink_contract = None

        for rpc_url in self.polygon_rpcs:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(CHAINLINK_BTC_USD),
                    abi=CHAINLINK_ABI,
                )
                round_data = contract.functions.latestRoundData().call()
                _, answer, _, updated_at, _ = round_data
                decimals = contract.functions.decimals().call()
                price = answer / (10 ** decimals)
                updated = datetime.utcfromtimestamp(updated_at)
                self._w3 = w3
                self._chainlink_contract = contract
                logger.info(f"Chainlink BTC/USD: ${price:,.2f} via {rpc_url}")
                return price, updated
            except Exception as e:
                logger.debug(f"Chainlink RPC {rpc_url} failed: {e}")
                continue

        logger.warning("Chainlink: all Polygon RPCs failed")
        return None, None

    # ──────────────────────────────────────────────────────────────────
    # Basic Indicators
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _calc_sma(series: pd.Series, period: int) -> pd.Series:
        return series.rolling(window=period, min_periods=1).mean()

    @staticmethod
    def _calc_wma(series: pd.Series, period: int) -> pd.Series:
        """Weighted Moving Average — linearly decreasing weights."""
        weights = np.arange(1, period + 1, dtype=float)
        return series.rolling(window=period, min_periods=period).apply(
            lambda x: np.dot(x, weights) / weights.sum(), raw=True
        )

    @classmethod
    def _calc_hma(cls, series: pd.Series, period: int) -> pd.Series:
        """Hull Moving Average: HMA(n) = WMA(2·WMA(n/2) − WMA(n), √n).
        Matches the Pine Script ta.hma() function exactly.
        """
        half = max(1, period // 2)
        sqrt_n = max(1, int(round(period ** 0.5)))
        wma_half = cls._calc_wma(series, half)
        wma_full = cls._calc_wma(series, period)
        delta = 2 * wma_half - wma_full
        return cls._calc_wma(delta, sqrt_n)

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

    @staticmethod
    def _find_support_resistance(
        df: pd.DataFrame, order: int = 10
    ) -> Tuple[List[float], List[float]]:
        """Find support/resistance levels using local extrema."""
        if len(df) < order * 2 + 1:
            return [], []
        high_vals = df["high"].values
        low_vals = df["low"].values
        resist_idx = argrelextrema(high_vals, np.greater, order=order)[0]
        support_idx = argrelextrema(low_vals, np.less, order=order)[0]
        resistance = sorted(set(round(high_vals[i], 0) for i in resist_idx))
        support = sorted(set(round(low_vals[i], 0) for i in support_idx))
        return support, resistance

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
    # Adaptive Trend Sabre (BOSWaves port)
    # Matches Pine Script defaults exactly:
    #   MA = HMA(34), trail = 3.25×ATR, snap_thresh = 2.0 ATR,
    #   level_buf = 0.25 ATR, max_levels = 8, retest_cd = 5
    # ──────────────────────────────────────────────────────────────────

    def calc_trend_sabre(self, df: pd.DataFrame,
                         ma_len: int = 34,
                         trail_factor: float = 3.25,
                         snap_thresh: float = 2.0,
                         level_buffer: float = 0.25,
                         max_levels: int = 4,
                         retest_cd: int = 8) -> TrendSabreResult:
        """Port of Adaptive Trend Sabre [BOSWaves] from Pine Script.

        Calculates:
        - SMA backbone with ATR-based trailing stop
        - Tension (stretch from MA in ATR units)
        - Snap levels (dynamic S/R from snap-back events)
        - Trend flip signals (bull/bear diamonds)
        - Retest detection
        """
        if len(df) < ma_len + 14:
            return TrendSabreResult()

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        # MA backbone — HMA(34), matching Pine Script default (ta.hma)
        ma = self._calc_hma(df["close"], ma_len).values

        # ATR(14)
        atr = self._calc_atr(df, 14).values

        n = len(close)

        # --- Trailing stop + trend direction (bar by bar) ---
        trend = np.zeros(n, dtype=int)
        trail = np.full(n, np.nan)

        # Initialize first valid bar
        start = max(ma_len, 14)
        if close[start] > ma[start]:
            trend[start] = 1
            trail[start] = ma[start] - atr[start] * trail_factor
        else:
            trend[start] = -1
            trail[start] = ma[start] + atr[start] * trail_factor

        for i in range(start + 1, n):
            raw_up = ma[i] - atr[i] * trail_factor
            raw_dn = ma[i] + atr[i] * trail_factor
            prev_trend = trend[i - 1]
            prev_trail = trail[i - 1]

            if prev_trend == 1:
                trail[i] = max(raw_up, prev_trail) if not np.isnan(prev_trail) else raw_up
                if close[i] < trail[i]:
                    trend[i] = -1
                    trail[i] = raw_dn
                else:
                    trend[i] = 1
            else:  # prev_trend == -1
                trail[i] = min(raw_dn, prev_trail) if not np.isnan(prev_trail) else raw_dn
                if close[i] > trail[i]:
                    trend[i] = 1
                    trail[i] = raw_up
                else:
                    trend[i] = -1

        # --- Tension ---
        tension = np.zeros(n)
        for i in range(n):
            if atr[i] > 0:
                tension[i] = (close[i] - ma[i]) / atr[i]

        # --- Snap levels (dynamic S/R from tension release) ---
        snap_supports = []
        snap_resistances = []
        was_snap_up = False
        was_snap_dn = False
        snap_high = 0.0
        snap_low = 0.0

        for i in range(start, n):
            t = tension[i]
            # Track stretch
            if t > snap_thresh:
                was_snap_up = True
                if high[i] > snap_high or not was_snap_up:
                    snap_high = high[i]
            if t < -snap_thresh:
                was_snap_dn = True
                if low[i] < snap_low or snap_low == 0:
                    snap_low = low[i]

            # Release detection — tension dropped below half threshold
            if was_snap_up and t < snap_thresh * 0.5:
                if snap_high > 0:
                    snap_resistances.append(round(snap_high, 0))
                    if len(snap_resistances) > max_levels:
                        snap_resistances = snap_resistances[-max_levels:]
                was_snap_up = False
                snap_high = 0.0

            if was_snap_dn and t > -snap_thresh * 0.5:
                if snap_low > 0:
                    snap_supports.append(round(snap_low, 0))
                    if len(snap_supports) > max_levels:
                        snap_supports = snap_supports[-max_levels:]
                was_snap_dn = False
                snap_low = 0.0

        # Remove broken levels
        current = close[-1]
        curr_atr = atr[-1]
        buf = curr_atr * level_buffer
        snap_supports = [s for s in snap_supports if current >= s - buf]
        snap_resistances = [r for r in snap_resistances if current <= r + buf]

        # --- Signals ---
        last = n - 1
        bull_signal = trend[last] == 1 and trend[last - 1] == -1 if last > 0 else False
        bear_signal = trend[last] == -1 and trend[last - 1] == 1 if last > 0 else False

        # --- Retest detection (last retest_cd bars) ---
        bull_retest = False
        bear_retest = False
        for i in range(max(start, last - retest_cd), last + 1):
            if trend[i] == 1 and low[i] <= trail[i] and close[i] > trail[i]:
                bull_retest = True
            if trend[i] == -1 and high[i] >= trail[i] and close[i] < trail[i]:
                bear_retest = True
            # Check snap level retests
            for s in snap_supports:
                if trend[i] == 1 and low[i] <= s and close[i] > s:
                    bull_retest = True
            for r in snap_resistances:
                if trend[i] == -1 and high[i] >= r and close[i] < r:
                    bear_retest = True

        return TrendSabreResult(
            ma_value=float(ma[-1]),
            trail_value=float(trail[-1]),
            trend=int(trend[-1]),
            tension=float(tension[-1]),
            tension_abs=float(abs(tension[-1])),
            atr=float(atr[-1]),
            bull_signal=bull_signal,
            bear_signal=bear_signal,
            snap_supports=snap_supports,
            snap_resistances=snap_resistances,
            bull_retest=bull_retest,
            bear_retest=bear_retest,
        )

    # ──────────────────────────────────────────────────────────────────
    # Candle Momentum Detection
    #
    # 15-min candle: strong signal if price spikes in first 4 min,
    #                prediction window at 9-12 min
    # 5-min candle:  strong signal if price spikes in first 1.5 min,
    #                prediction window at 3-4 min
    # ──────────────────────────────────────────────────────────────────

    def calc_candle_momentum(self) -> CandleMomentum:
        """Detect early-candle momentum and check if we're in prediction window.

        Uses 1-minute klines to see intra-candle price action within the
        current 15m and 5m candles.
        """
        result = CandleMomentum()

        # Fetch recent 1-minute candles (last 20 gives us enough)
        df_1m = self.fetch_klines("1m", 20)
        if df_1m.empty or len(df_1m) < 5:
            return result

        now_utc = datetime.utcnow()

        # --- 15-minute candle analysis ---
        # Find the start of the current 15-minute candle
        current_minute = now_utc.minute
        m15_start_minute = (current_minute // 15) * 15
        m15_candle_start = now_utc.replace(minute=m15_start_minute, second=0, microsecond=0)
        m15_age_minutes = (now_utc - m15_candle_start).total_seconds() / 60.0
        result.m15_candle_age_minutes = m15_age_minutes

        # Get 1m candles within this 15m candle
        m15_candles = df_1m[df_1m["open_time"] >= m15_candle_start]
        if len(m15_candles) >= 2:
            candle_open = float(m15_candles["open"].iloc[0])

            # First 4 minutes — check for spike
            early_candles = m15_candles[
                m15_candles["open_time"] < m15_candle_start + timedelta(minutes=4)
            ]
            if len(early_candles) >= 1:
                early_close = float(early_candles["close"].iloc[-1])
                early_high = float(early_candles["high"].max())
                early_low = float(early_candles["low"].min())
                move_pct = (early_close - candle_open) / candle_open * 100
                result.m15_move_pct = move_pct

                # Spike: >0.15% move in first 4 min is significant for BTC
                if move_pct > 0.15:
                    result.m15_direction = "SPIKE_UP"
                elif move_pct < -0.15:
                    result.m15_direction = "SPIKE_DOWN"
                # Drift: consistent direction (all closes same side of open)
                elif move_pct > 0.05:
                    result.m15_direction = "DRIFT_UP"
                elif move_pct < -0.05:
                    result.m15_direction = "DRIFT_DOWN"

            # Prediction window: 9-12 minutes into the candle
            result.m15_in_prediction_window = 9.0 <= m15_age_minutes <= 12.0

        # --- 5-minute candle analysis ---
        m5_start_minute = (current_minute // 5) * 5
        m5_candle_start = now_utc.replace(minute=m5_start_minute, second=0, microsecond=0)
        m5_age_minutes = (now_utc - m5_candle_start).total_seconds() / 60.0
        result.m5_candle_age_minutes = m5_age_minutes

        m5_candles = df_1m[df_1m["open_time"] >= m5_candle_start]
        if len(m5_candles) >= 1:
            candle_open = float(m5_candles["open"].iloc[0])

            # First 1.5 minutes — check for spike (scaled from 4/15 ratio)
            early_5m = m5_candles[
                m5_candles["open_time"] < m5_candle_start + timedelta(seconds=90)
            ]
            if len(early_5m) >= 1:
                early_close = float(early_5m["close"].iloc[-1])
                move_pct = (early_close - candle_open) / candle_open * 100
                result.m5_move_pct = move_pct

                if move_pct > 0.08:
                    result.m5_direction = "SPIKE_UP"
                elif move_pct < -0.08:
                    result.m5_direction = "SPIKE_DOWN"
                elif move_pct > 0.03:
                    result.m5_direction = "DRIFT_UP"
                elif move_pct < -0.03:
                    result.m5_direction = "DRIFT_DOWN"

            # Prediction window: 3-4 minutes into the candle
            result.m5_in_prediction_window = 3.0 <= m5_age_minutes <= 4.0

        # --- Combined momentum signal ---
        up_signals = 0
        down_signals = 0
        for d in [result.m15_direction, result.m5_direction]:
            if "UP" in d:
                up_signals += 2 if "SPIKE" in d else 1
            elif "DOWN" in d:
                down_signals += 2 if "SPIKE" in d else 1

        if up_signals >= 3:
            result.momentum_signal = "STRONG_UP"
            result.momentum_strength = min(1.0, up_signals / 4.0)
        elif up_signals >= 1 and down_signals == 0:
            result.momentum_signal = "LEAN_UP"
            result.momentum_strength = 0.4
        elif down_signals >= 3:
            result.momentum_signal = "STRONG_DOWN"
            result.momentum_strength = min(1.0, down_signals / 4.0)
        elif down_signals >= 1 and up_signals == 0:
            result.momentum_signal = "LEAN_DOWN"
            result.momentum_strength = 0.4
        else:
            result.momentum_signal = "NEUTRAL"
            result.momentum_strength = 0.0

        return result

    # ──────────────────────────────────────────────────────────────────
    # EMA Trend Determination
    # ──────────────────────────────────────────────────────────────────

    def _determine_trend(self, df: pd.DataFrame) -> Tuple[str, float]:
        """Determine trend direction and strength from EMA alignment."""
        if len(df) < 50:
            return "NEUTRAL", 0.0

        close = df["close"]
        ema_9 = self._calc_ema(close, 9).iloc[-1]
        ema_21 = self._calc_ema(close, 21).iloc[-1]
        ema_50 = self._calc_ema(close, 50).iloc[-1]
        price = close.iloc[-1]

        bullish_score = 0
        if price > ema_9:
            bullish_score += 1
        if ema_9 > ema_21:
            bullish_score += 1
        if ema_21 > ema_50:
            bullish_score += 1

        if bullish_score >= 3:
            return "BULLISH", bullish_score / 3.0
        elif bullish_score <= 0:
            bearish_score = sum([price < ema_9, ema_9 < ema_21, ema_21 < ema_50])
            return "BEARISH", bearish_score / 3.0
        else:
            return "NEUTRAL", 0.3

    # ──────────────────────────────────────────────────────────────────
    # Anchored Volume Profile
    # ──────────────────────────────────────────────────────────────────

    def calc_anchored_volume_profile(
        self, df: pd.DataFrame, anchor_idx: int = None, num_bins: int = 50,
        sabre_result: TrendSabreResult = None,
    ) -> AnchoredVolumeProfile:
        """Calculate volume profile anchored to a key swing point.

        The anchor is the swing point that started the current trend move:
        - In a BULL trend: anchor at the swing LOW that launched the move up
        - In a BEAR trend: anchor at the swing HIGH that started the decline

        This tells us where meaningful volume participation happened since the
        trend began, so we can gauge:
        - Is price above HVN support? (strong conviction for longs)
        - Is price stuck in the value area? (reduce exposure, wait for breakout)
        - Is price in a low-volume zone? (fast moves expected)
        """
        if len(df) < 20:
            return AnchoredVolumeProfile()

        if anchor_idx is None:
            current_trend = sabre_result.trend if sabre_result else 1

            # Strategy: find the swing point that STARTED the current trend
            # Walk backwards through the data to find where trend began
            lookback = min(80, len(df))
            recent = df.tail(lookback)
            low_vals = recent["low"].values
            high_vals = recent["high"].values
            close_vals = recent["close"].values

            # Find swing lows and highs with order=5 (5 bars each side)
            try:
                swing_low_idx = argrelextrema(low_vals, np.less, order=5)[0]
                swing_high_idx = argrelextrema(high_vals, np.greater, order=5)[0]
            except Exception:
                swing_low_idx = np.array([])
                swing_high_idx = np.array([])

            if current_trend == 1 and len(swing_low_idx) > 0:
                # BULL trend: anchor at the most recent significant swing LOW
                # This is where the uptrend launched from
                rel_idx = swing_low_idx[-1]
                anchor_idx = len(df) - lookback + rel_idx
                anchor_type = "trend_start_low"
                anchor_price = float(low_vals[rel_idx])
            elif current_trend == -1 and len(swing_high_idx) > 0:
                # BEAR trend: anchor at the most recent significant swing HIGH
                # This is where the downtrend started
                rel_idx = swing_high_idx[-1]
                anchor_idx = len(df) - lookback + rel_idx
                anchor_type = "trend_start_high"
                anchor_price = float(high_vals[rel_idx])
            elif len(swing_low_idx) > 0:
                rel_idx = swing_low_idx[-1]
                anchor_idx = len(df) - lookback + rel_idx
                anchor_type = "swing_low"
                anchor_price = float(low_vals[rel_idx])
            elif len(swing_high_idx) > 0:
                rel_idx = swing_high_idx[-1]
                anchor_idx = len(df) - lookback + rel_idx
                anchor_type = "swing_high"
                anchor_price = float(high_vals[rel_idx])
            else:
                anchor_idx = max(0, len(df) - 30)
                anchor_type = "default_30bars"
                anchor_price = float(df["close"].iloc[anchor_idx])
        else:
            anchor_type = "manual"
            anchor_price = float(df["close"].iloc[anchor_idx])

        # Slice from anchor to present
        sliced = df.iloc[anchor_idx:].copy()
        if len(sliced) < 3:
            return AnchoredVolumeProfile()

        # Build price bins
        price_low = float(sliced["low"].min())
        price_high = float(sliced["high"].max())
        if price_high <= price_low:
            return AnchoredVolumeProfile()

        bin_edges = np.linspace(price_low, price_high, num_bins + 1)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        bin_volumes = np.zeros(num_bins)

        # Distribute each candle's volume across the price bins it touched
        for _, row in sliced.iterrows():
            candle_low = row["low"]
            candle_high = row["high"]
            candle_vol = row["volume"]
            if candle_vol <= 0 or candle_high <= candle_low:
                continue

            # Find which bins this candle overlaps
            for b in range(num_bins):
                bin_lo = bin_edges[b]
                bin_hi = bin_edges[b + 1]
                # Overlap between candle range and bin range
                overlap_lo = max(candle_low, bin_lo)
                overlap_hi = min(candle_high, bin_hi)
                if overlap_hi > overlap_lo:
                    # Proportion of candle's range that falls in this bin
                    proportion = (overlap_hi - overlap_lo) / (candle_high - candle_low)
                    bin_volumes[b] += candle_vol * proportion

        total_vol = bin_volumes.sum()
        if total_vol == 0:
            return AnchoredVolumeProfile()

        # POC — price bin with highest volume
        poc_idx = int(np.argmax(bin_volumes))
        poc_price = float(bin_centers[poc_idx])

        # Value Area — 70% of total volume centered around POC
        value_area_target = total_vol * 0.70
        va_vol = bin_volumes[poc_idx]
        lo_idx = poc_idx
        hi_idx = poc_idx

        while va_vol < value_area_target and (lo_idx > 0 or hi_idx < num_bins - 1):
            expand_lo = bin_volumes[lo_idx - 1] if lo_idx > 0 else 0
            expand_hi = bin_volumes[hi_idx + 1] if hi_idx < num_bins - 1 else 0
            if expand_lo >= expand_hi and lo_idx > 0:
                lo_idx -= 1
                va_vol += bin_volumes[lo_idx]
            elif hi_idx < num_bins - 1:
                hi_idx += 1
                va_vol += bin_volumes[hi_idx]
            else:
                lo_idx -= 1
                va_vol += bin_volumes[lo_idx]

        val_price = float(bin_centers[lo_idx])
        vah_price = float(bin_centers[hi_idx])

        # High/Low Volume Nodes
        mean_vol = total_vol / num_bins
        hvn = [float(bin_centers[i]) for i in range(num_bins)
               if bin_volumes[i] > mean_vol * 1.5]
        lvn = [float(bin_centers[i]) for i in range(num_bins)
               if 0 < bin_volumes[i] < mean_vol * 0.5]

        return AnchoredVolumeProfile(
            poc_price=poc_price,
            vah_price=vah_price,
            val_price=val_price,
            high_volume_nodes=hvn[-5:],
            low_volume_nodes=lvn[-5:],
            anchor_type=anchor_type,
            anchor_price=anchor_price,
            total_volume=float(total_vol),
            num_bins=num_bins,
        )

    # ──────────────────────────────────────────────────────────────────
    # Full Analysis (everything combined)
    # ──────────────────────────────────────────────────────────────────

    def get_full_analysis(self) -> Optional[TechnicalAnalysis]:
        """Perform full multi-timeframe technical analysis on BTC.

        Fetches 1m, 5m, 15m, 1h, 4h, 1d data and runs:
        - MACD on 15m and 4h
        - Trend Sabre on 4h
        - Candle momentum on 1m data
        - EMA/RSI on 4h
        - S/R from daily + snap levels
        """
        # Fetch all timeframes
        df_1h = self.fetch_klines("1h", 200)
        df_4h = self.fetch_klines("4h", 200)
        df_1d = self.fetch_klines("1d", 200)
        df_15m = self.fetch_klines("15m", 100)

        if df_1h.empty or df_4h.empty or df_1d.empty:
            logger.warning("Could not fetch BTC klines for analysis")
            return None

        current_price = float(df_1h["close"].iloc[-1])

        # --- EMAs from 4h ---
        close_4h = df_4h["close"]
        ema_9 = float(self._calc_ema(close_4h, 9).iloc[-1])
        ema_21 = float(self._calc_ema(close_4h, 21).iloc[-1])
        ema_50 = float(self._calc_ema(close_4h, 50).iloc[-1])
        ema_200 = float(self._calc_ema(close_4h, 200).iloc[-1]) if len(df_4h) >= 200 else 0.0

        # --- RSI from 4h ---
        rsi_14 = float(self._calc_rsi(close_4h, 14).iloc[-1])

        # --- MACD on 4h (higher TF trend filter) ---
        macd_4h = self.calc_macd(df_4h, fast=12, slow=26, signal=9)

        # --- MACD on 1h (intermediate TF — fallback gate when 4H is decelerating) ---
        macd_1h = self.calc_macd(df_1h, fast=12, slow=26, signal=9)

        # --- MACD on 15m (lower TF entry confirmation) ---
        macd_15m = self.calc_macd(df_15m, fast=12, slow=26, signal=9) if not df_15m.empty else MACDResult()

        # --- Adaptive Trend Sabre on 4h ---
        sabre = self.calc_trend_sabre(
            df_4h, ma_len=35, trail_factor=3.25,
            snap_thresh=2.0, level_buffer=0.25, max_levels=4, retest_cd=8,
        )

        # --- Candle Momentum from 1m data ---
        candle_mom = self.calc_candle_momentum()

        # --- Multi-timeframe trends ---
        h1_trend, _ = self._determine_trend(df_1h)
        h4_trend, h4_strength = self._determine_trend(df_4h)
        daily_trend, _ = self._determine_trend(df_1d)

        # --- Overall trend (weighted: daily > 4h > 1h + MACD + Sabre) ---
        trends = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
        trends[daily_trend] += 3
        trends[h4_trend] += 2
        trends[h1_trend] += 1

        # MACD 4H adds weight (higher TF = more weight)
        if macd_4h.above_zero and macd_4h.histogram_rising:
            trends["BULLISH"] += 2
        elif not macd_4h.above_zero and not macd_4h.histogram_rising:
            trends["BEARISH"] += 2
        if macd_4h.crossover == "BULLISH_CROSS":
            trends["BULLISH"] += 1
        elif macd_4h.crossover == "BEARISH_CROSS":
            trends["BEARISH"] += 1

        # Trend Sabre adds weight
        if sabre.trend == 1:
            trends["BULLISH"] += 2
        elif sabre.trend == -1:
            trends["BEARISH"] += 2
        if sabre.bull_signal:
            trends["BULLISH"] += 1
        if sabre.bear_signal:
            trends["BEARISH"] += 1

        # Candle momentum adds weight
        if "UP" in candle_mom.momentum_signal:
            trends["BULLISH"] += 1
        elif "DOWN" in candle_mom.momentum_signal:
            trends["BEARISH"] += 1

        total_weight = sum(trends.values()) or 1
        overall = max(trends, key=trends.get)
        strength = trends[overall] / total_weight

        # --- Support/Resistance: merge swing levels + snap levels ---
        swing_support, swing_resistance = self._find_support_resistance(df_1d, order=5)
        all_support = sorted(set(swing_support + sabre.snap_supports))
        all_resistance = sorted(set(swing_resistance + sabre.snap_resistances))

        nearest_support = max([s for s in all_support if s < current_price], default=0.0)
        nearest_resistance = min([r for r in all_resistance if r > current_price], default=0.0)

        # --- Anchored Volume Profile on 4H (anchored to trend-start swing) ---
        vol_profile = self.calc_anchored_volume_profile(
            df_4h, num_bins=50, sabre_result=sabre
        )

        # --- Chainlink ---
        cl_price, cl_updated = self.get_chainlink_price()

        analysis = TechnicalAnalysis(
            current_price=current_price,
            ema_9=ema_9,
            ema_21=ema_21,
            ema_50=ema_50,
            ema_200=ema_200,
            rsi_14=rsi_14,
            macd_4h=macd_4h,
            macd_1h=macd_1h,
            macd_15m=macd_15m,
            trend_sabre=sabre,
            candle_momentum=candle_mom,
            trend_direction=overall,
            trend_strength=strength,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            support_levels=all_support[-5:],
            resistance_levels=all_resistance[-5:],
            daily_trend=daily_trend,
            h4_trend=h4_trend,
            h1_trend=h1_trend,
            volume_profile=vol_profile,
            chainlink_price=cl_price,
            chainlink_updated_at=cl_updated,
        )

        logger.info(
            f"BTC ${current_price:,.0f} | {overall} ({strength:.0%}) | "
            f"MACD4H={macd_4h.histogram:+.0f} {'RISING' if macd_4h.histogram_rising else 'FALLING'} {macd_4h.crossover} | "
            f"MACD1H={macd_1h.histogram:+.2f} {'RISING' if macd_1h.histogram_rising else 'FALLING'} | "
            f"MACD15m={macd_15m.histogram:+.0f} {macd_15m.crossover} | "
            f"Sabre={'BULL' if sabre.trend==1 else 'BEAR'} trail=${sabre.trail_value:,.0f} tension={sabre.tension:+.1f} | "
            f"RSI={rsi_14:.0f} | Mom={candle_mom.momentum_signal} | "
            f"VP POC=${vol_profile.poc_price:,.0f} VAH=${vol_profile.vah_price:,.0f} VAL=${vol_profile.val_price:,.0f} | "
            f"S=${nearest_support:,.0f} R=${nearest_resistance:,.0f}"
        )

        return analysis
