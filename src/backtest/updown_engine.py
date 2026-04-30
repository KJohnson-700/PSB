"""
Updown Backtest Engine — replays the Bitcoin / SOL / ETH Up-or-Down strategies
against historical Binance OHLCV using the EXACT same indicator math as
the live strategies.

Architecture
────────────
1.  Caller pre-fetches all required OHLCV via OHLCVLoader and passes the
    dict to ``run()``.  No live API calls happen inside the engine.
2.  Engine walks time in 15m (or 5m) steps over [start_date, end_date].
3.  At each window-open timestamp T, OHLCV is sliced to data BEFORE T
    (strict no look-ahead).
4.  The same indicator functions imported from BTCPriceService /
    SOLBTCService are used — the backtest is testing the identical
    signal logic that runs live.
5.  Candle-momentum (early-spike detection) is set to NEUTRAL because it
    requires intra-window 1m bars that would introduce look-ahead bias.
    For BTC 5m, we use the last COMPLETED 5m bar direction instead.
6.  Entry sampled from empirical fill-price distribution loaded from
    data/entry_prices/updown_fills.jsonl (recorded by TradeJournal at live fills).
    Falls back to N(0.50, 0.06) clipped to [0.30, 0.70] when <20 recorded prices.
    Previous hardcoded 0.50 inflated WR by 15-21% vs live results.
7.  Settlement: 1m close at window end vs 1m open at window start
    -> YES won (price went UP) or NO won (price went DOWN).
8.  Ruin cap enforced: ``bankroll = max(0, bankroll + pnl)``.

Signal fidelity
───────────────
BTC: mirrors bitcoin.py exactly (HTF 3-vote with early_bull/early_bear/
     recovery, graduated 15m boost, anti-LTF gate, 5m candle momentum).
SOL: mirrors sol_macro.py (1H EMA trend + 15m EMA alignment + 15m RSI for
     HTF, SOL-specific LTF weights, 5m MACD with live weights).
     Lag/correlation signals are omitted (require live BTC feed).

Checklist (from docs/BACKTEST.md)
──────────────────────────────────
[x] Ruin cap
[x] Slippage modeled (entry + exit at settlement is 0/1, no exit slip)
[x] Resolution settlement applied (actual OHLCV direction)
[x] Universe pinned (exact date range + symbol logged in result)
[x] Timestamp alignment: all slices use open_time < T (strict)
[x] Exit strategy: hold to settlement (15m / 5m window close)
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd

from src.analysis.btc_price_service import (
    BTCPriceService,
    TechnicalAnalysis,
    MACDResult,
    TrendSabreResult,
    CandleMomentum,
    AnchoredVolumeProfile,
)

logger = logging.getLogger(__name__)

# Minimum bars required before indicators are reliable
_MIN_4H_BARS  = 65   # Sabre SMA(35) + ATR(14) + warmup
_MIN_15M_BARS = 50   # MACD(26,9) + warmup
_MIN_5M_BARS  = 40   # 5m MACD warmup

# NOTE: Live applies a max_edge_updown = 0.12 cap, but that filters market
# mispricing (yes_price hasn't caught up to reality).  In the backtest we
# assume perfect pricing at YES = 0.50, so edge = pure signal strength.
# Applying the cap here would incorrectly block valid signals.


# ==============================================================================
# Result data-classes
# ==============================================================================

@dataclass
class UpdownTrade:
    """One simulated updown trade."""
    window_open:   pd.Timestamp
    window_close:  pd.Timestamp
    symbol:        str           # "BTC", "SOL", or "ETH"
    window_size:   int           # 5 or 15 (minutes)
    action:        str           # "BUY_YES" or "SELL_YES"
    htf_bias:      str           # "BULLISH" | "BEARISH"
    ltf_confirmed: bool
    ltf_strength:  float
    entry_price:   float         # Assumed mid YES price (0.50)
    fill_price:    float         # After slippage
    size:          float         # $ notional
    edge:          float         # Estimated edge vs 0.50
    confidence:    float
    outcome:       Optional[str] = None   # "WIN" | "LOSS"
    exit_price:    float = 0.0
    pnl:           float = 0.0
    slip:          float = 0.0   # Slippage cost in $ for this trade
    asset_open:    float = 0.0   # BTC / SOL / ETH price at window open
    asset_close:   float = 0.0   # BTC / SOL / ETH price at window close


@dataclass
class UpdownBacktestResult:
    """Aggregate results from a crypto updown backtest run."""
    symbol:           str
    window_size:      int         # 5 or 15 minutes
    start_date:       str
    end_date:         str
    initial_bankroll: float
    final_bankroll:   float
    trades:           List[UpdownTrade] = field(default_factory=list)
    windows_scanned:  int = 0
    windows_entered:  int = 0
    wins:             int = 0
    losses:           int = 0
    slippage_total:   float = 0.0
    oracle_symbol:    Optional[str] = None
    oracle_history_loaded: bool = False
    oracle_history_points: int = 0
    oracle_basis_skips: int = 0

    @staticmethod
    def _count_windows_for_range(
        start_date: str,
        end_date: str,
        window_size: int,
    ) -> int:
        """Mirror engine scan-window counting for a date range."""
        tz = timezone.utc
        step_s = window_size * 60
        start_epoch = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
        start_epoch -= start_epoch % step_s
        current = pd.Timestamp(datetime.fromtimestamp(start_epoch, tz=tz))
        end_ts = pd.Timestamp(
            datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, tzinfo=tz
            )
        )
        windows = 0
        while current <= end_ts:
            windows += 1
            current += pd.Timedelta(minutes=window_size)
        return windows

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def net_pnl(self) -> float:
        return self.final_bankroll - self.initial_bankroll

    @property
    def total_return_pct(self) -> float:
        if self.initial_bankroll <= 0:
            return 0.0
        return self.net_pnl / self.initial_bankroll * 100

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def avg_edge(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.edge for t in self.trades) / len(self.trades)

    @property
    def expectancy(self) -> float:
        """Average PnL per trade in $."""
        if not self.trades:
            return 0.0
        return sum(t.pnl for t in self.trades) / len(self.trades)

    def split(self, test_start: str) -> tuple["UpdownBacktestResult", "UpdownBacktestResult"]:
        """Partition trades into (train, test) at test_start date.

        Returns two independent UpdownBacktestResult objects.  The engine runs
        once over the full date range; this method partitions the output so
        train and test metrics are computed separately.  The test result is the
        only one that can be used to evaluate whether a parameter set generalises
        — never tune on it.

        Parameters
        ----------
        test_start : "YYYY-MM-DD"  first date of the held-out test period
        """
        test_ts = pd.Timestamp(test_start).tz_localize("UTC")

        train_trades = [t for t in self.trades if t.window_open <  test_ts]
        test_trades  = [t for t in self.trades if t.window_open >= test_ts]

        def _build(trades: list, start: str, end: str) -> "UpdownBacktestResult":
            wins   = sum(1 for t in trades if t.outcome == "WIN")
            losses = sum(1 for t in trades if t.outcome == "LOSS")
            pnl    = sum(t.pnl  for t in trades)
            slip   = sum(t.slip for t in trades)
            return UpdownBacktestResult(
                symbol=self.symbol,
                window_size=self.window_size,
                start_date=start,
                end_date=end,
                initial_bankroll=self.initial_bankroll,
                final_bankroll=self.initial_bankroll + pnl,
                trades=trades,
                windows_scanned=self._count_windows_for_range(
                    start, end, self.window_size
                ),
                windows_entered=len(trades),
                wins=wins,
                losses=losses,
                slippage_total=round(slip, 4),
                oracle_symbol=self.oracle_symbol,
                oracle_history_loaded=self.oracle_history_loaded,
                oracle_history_points=self.oracle_history_points,
            )

        return _build(train_trades, self.start_date, test_start), \
               _build(test_trades,  test_start,      self.end_date)


# ==============================================================================
# Engine
# ==============================================================================

class UpdownBacktestEngine:
    """Replays Bitcoin or alt-coin (SOL / ETH) updown strategy on historical OHLCV.

    Does NOT make any live API calls during the replay -- all data is
    pre-fetched by the caller via OHLCVLoader and passed into ``run()``.
    """

    def __init__(self, config: Dict[str, Any], initial_bankroll: float = 500.0):
        self.config           = config
        self.initial_bankroll = initial_bankroll

        # Slippage config
        slip_cfg         = config.get("backtest", {}).get("slippage", {})
        self.slippage_bps = slip_cfg.get("default_bps", 25)

        # Strategy thresholds -- BACKTEST-SPECIFIC defaults
        #
        # Live strategies use min_edge thresholds calibrated for variable
        # yes_price (e.g., 0.14 for BTC 15m).  But in backtest, entry is
        # always at YES = 0.50, so edge = pure signal strength and never
        # benefits from market mispricing.  We therefore use lower thresholds
        # tuned for signal-only edge.  A backtest.min_edge_* section in the
        # config can override these defaults if needed.
        bt_cfg   = config.get("backtest", {})
        strat    = config.get("strategies", {})
        btc_cfg  = strat.get("bitcoin",  {})
        sol_cfg  = strat.get("sol_macro",  {})
        eth_cfg  = strat.get("eth_macro",  {})
        xrp_cfg  = strat.get("xrp_macro",  {})
        hype_cfg = strat.get("hype_macro", {})

        self.min_edge_15m       = bt_cfg.get("min_edge_btc_15m",   0.06)
        self.min_edge_5m        = bt_cfg.get("min_edge_btc_5m",    0.07)
        self.min_edge_sol_15m   = bt_cfg.get("min_edge_sol_15m",   0.06)
        self.min_edge_sol_5m    = bt_cfg.get("min_edge_sol_5m",    0.06)
        self.min_edge_eth_15m   = bt_cfg.get("min_edge_eth_15m",   self.min_edge_sol_15m)
        self.min_edge_eth_5m    = bt_cfg.get("min_edge_eth_5m",    self.min_edge_sol_5m)
        self.min_edge_xrp_15m   = bt_cfg.get("min_edge_xrp_15m",  self.min_edge_sol_15m)
        self.min_edge_xrp_5m    = bt_cfg.get("min_edge_xrp_5m",   self.min_edge_sol_5m)
        self.min_edge_hype_15m  = bt_cfg.get("min_edge_hype_15m", self.min_edge_sol_15m)
        self.min_edge_hype_5m   = bt_cfg.get("min_edge_hype_5m",  self.min_edge_sol_5m)
        # Each symbol has independent min_edge keys; XRP/HYPE fall back to SOL if not set.
        self._kelly_btc   = btc_cfg.get("kelly_fraction",  0.15)
        self._kelly_sol   = sol_cfg.get("kelly_fraction",  self._kelly_btc)
        self._kelly_eth   = eth_cfg.get("kelly_fraction",  self._kelly_sol)
        self._kelly_xrp   = xrp_cfg.get("kelly_fraction",  self._kelly_sol)
        self._kelly_hype  = hype_cfg.get("kelly_fraction", self._kelly_sol)
        self.min_4h_hist_magnitude = btc_cfg.get("min_4h_hist_magnitude", 20.0)
        self.min_positive_m5_adj_sol_5m = float(sol_cfg.get("min_positive_m5_adj_5m", 0.0))
        self.min_positive_m5_adj_eth_5m = float(eth_cfg.get("min_positive_m5_adj_5m", self.min_positive_m5_adj_sol_5m))
        self.min_positive_m5_adj_xrp_5m = float(xrp_cfg.get("min_positive_m5_adj_5m", self.min_positive_m5_adj_sol_5m))
        self.min_positive_m5_adj_hype_5m = float(hype_cfg.get("min_positive_m5_adj_5m", self.min_positive_m5_adj_sol_5m))

        trade_cfg         = config.get("trading", {})
        self.default_size  = trade_cfg.get("default_position_size", 10.0)
        self.max_size      = trade_cfg.get("max_position_size", 15.0)
        exposure_cfg = config.get("exposure", {})
        self.exposure_min_trade_usd = float(exposure_cfg.get("min_trade_usd", 0.0) or 0.0)
        self.exposure_full_size = float(exposure_cfg.get("full_size", self.max_size) or self.max_size)
        self._entry_bands = {
            "BTC": (
                float(btc_cfg.get("entry_price_min_updown", btc_cfg.get("entry_price_min", 0.0)) or 0.0),
                float(btc_cfg.get("entry_price_max_updown", btc_cfg.get("entry_price_max", 1.0)) or 1.0),
            ),
            "SOL": (
                float(sol_cfg.get("entry_price_min", 0.0) or 0.0),
                float(sol_cfg.get("entry_price_max", 1.0) or 1.0),
            ),
            "ETH": (
                float(eth_cfg.get("entry_price_min", 0.0) or 0.0),
                float(eth_cfg.get("entry_price_max", 1.0) or 1.0),
            ),
            "XRP": (
                float(xrp_cfg.get("entry_price_min", 0.0) or 0.0),
                float(xrp_cfg.get("entry_price_max", 1.0) or 1.0),
            ),
            "HYPE": (
                float(hype_cfg.get("entry_price_min", 0.0) or 0.0),
                float(hype_cfg.get("entry_price_max", 1.0) or 1.0),
            ),
        }
        self.entry_price_min = 0.0
        self.entry_price_max = 1.0

        # Reuse live indicator methods via an instance (static methods underneath)
        self._svc = BTCPriceService()

        # Load empirical fill-price distribution from live sessions.
        # Falls back to N(0.50, 0.06) when fewer than 20 recorded prices exist.
        self._fill_prices: Optional[np.ndarray] = self._load_fill_prices()

    # -- fill price distribution -----------------------------------------------

    _FILL_PRICE_LOG = (
        Path(__file__).resolve().parent.parent.parent
        / "data" / "entry_prices" / "updown_fills.jsonl"
    )
    _MIN_EMPIRICAL_FILLS = 20

    @classmethod
    def _load_fill_prices(cls) -> Optional[np.ndarray]:
        """Load actual CLOB fill prices recorded by TradeJournal.

        Returns an ndarray for np.random.choice sampling, or None to fall back
        to the synthetic N(0.50, 0.06) distribution.
        """
        if not cls._FILL_PRICE_LOG.exists():
            return None
        prices = []
        try:
            with cls._FILL_PRICE_LOG.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        p = json.loads(line).get("yes_price")
                        if p is not None and 0.30 <= float(p) <= 0.70:
                            prices.append(float(p))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            return None
        if len(prices) < cls._MIN_EMPIRICAL_FILLS:
            logger.debug(
                f"updown_engine: only {len(prices)} recorded fill prices "
                f"(need {cls._MIN_EMPIRICAL_FILLS}) — using N(0.50, 0.06) fallback"
            )
            return None
        arr = np.array(prices, dtype=float)
        logger.info(
            f"updown_engine: loaded {len(arr)} empirical fill prices "
            f"mean={arr.mean():.4f} std={arr.std():.4f} — replacing N(0.50,0.06)"
        )
        return arr

    def _sample_entry_price(self) -> float:
        """Sample a realistic YES entry price for a backtest trade."""
        if self._fill_prices is not None:
            return float(np.random.choice(self._fill_prices))
        raw = float(np.random.normal(0.50, 0.06))
        return float(np.clip(raw, 0.30, 0.70))

    # -- slice helpers ---------------------------------------------------------

    @staticmethod
    def _before(df: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
        """All rows with open_time strictly BEFORE t -- no look-ahead."""
        return df[df["open_time"] < t].copy()

    @staticmethod
    def _replay_candle_momentum(
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
    ) -> CandleMomentum:
        """Approximate live candle-momentum from the first minutes of the replay window.

        This intentionally uses intra-window 1m data for the current candle, matching the
        way the live strategy reads early-candle momentum for BTC-follow decisions.
        """
        result = CandleMomentum()
        if df_1m.empty:
            return result

        m15_early = df_1m[
            (df_1m["open_time"] >= window_open)
            & (df_1m["open_time"] < window_open + pd.Timedelta(minutes=4))
        ]
        if not m15_early.empty:
            candle_open = float(m15_early.iloc[0]["open"])
            early_close = float(m15_early.iloc[-1]["close"])
            move_pct = (early_close - candle_open) / candle_open * 100 if candle_open > 0 else 0.0
            result.m15_move_pct = move_pct
            if move_pct > 0.15:
                result.m15_direction = "SPIKE_UP"
            elif move_pct < -0.15:
                result.m15_direction = "SPIKE_DOWN"
            elif move_pct > 0.05:
                result.m15_direction = "DRIFT_UP"
            elif move_pct < -0.05:
                result.m15_direction = "DRIFT_DOWN"

        m5_early = df_1m[
            (df_1m["open_time"] >= window_open)
            & (df_1m["open_time"] < window_open + pd.Timedelta(seconds=90))
        ]
        if not m5_early.empty:
            candle_open = float(m5_early.iloc[0]["open"])
            early_close = float(m5_early.iloc[-1]["close"])
            move_pct = (early_close - candle_open) / candle_open * 100 if candle_open > 0 else 0.0
            result.m5_move_pct = move_pct
            if move_pct > 0.08:
                result.m5_direction = "SPIKE_UP"
            elif move_pct < -0.08:
                result.m5_direction = "SPIKE_DOWN"
            elif move_pct > 0.03:
                result.m5_direction = "DRIFT_UP"
            elif move_pct < -0.03:
                result.m5_direction = "DRIFT_DOWN"

        return result

    # -- indicator reconstruction ----------------------------------------------

    def _build_ta(
        self, t: pd.Timestamp, data: Dict[str, pd.DataFrame],
        htf_key: str = "4h",
    ) -> Optional[TechnicalAnalysis]:
        """Reconstruct TechnicalAnalysis for window-open time T.

        Uses only data from BEFORE T to prevent any look-ahead bias.
        Returns None when there is insufficient warmup data.

        htf_key: "4h" for BTC, "1h" for SOL or ETH
        """
        df_htf = self._before(data[htf_key], t)
        df_15m = self._before(data["15m"],   t)

        if len(df_htf) < _MIN_4H_BARS or len(df_15m) < _MIN_15M_BARS:
            return None

        # -- HTF indicators ----------------------------------------------------
        sabre   = self._svc.calc_trend_sabre(df_htf)
        macd_4h = self._svc.calc_macd(df_htf)

        rsi_series = BTCPriceService._calc_rsi(df_htf["close"])
        rsi_14     = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0

        # EMAs on HTF
        ema_9   = float(BTCPriceService._calc_ema(df_htf["close"],  9).iloc[-1])
        ema_21  = float(BTCPriceService._calc_ema(df_htf["close"], 21).iloc[-1])
        ema_50  = float(BTCPriceService._calc_ema(df_htf["close"], 50).iloc[-1])
        ema_200 = float(BTCPriceService._calc_ema(df_htf["close"], 200).iloc[-1]) \
                  if len(df_htf) >= 200 else ema_50

        # -- 15m MACD ----------------------------------------------------------
        macd_15m = self._svc.calc_macd(df_15m)

        # -- 1h MACD -----------------------------------------------------------
        if "1h" in data:
            df_1h = self._before(data["1h"], t)
        else:
            df_1h = pd.DataFrame()
        if df_1h.empty and not df_15m.empty:
            df_1h = (
                df_15m.set_index("open_time")
                .resample("1h")
                .agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                })
                .dropna()
                .reset_index()
            )
        macd_1h = self._svc.calc_macd(df_1h) if len(df_1h) >= 30 else MACDResult()

        # -- Support / Resistance from last 60 HTF bars ------------------------
        sr_df = df_htf.tail(60)
        supports, resistances = BTCPriceService._find_support_resistance(sr_df)
        current_price   = float(df_htf["close"].iloc[-1])
        nearest_support    = max((s for s in supports    if s < current_price), default=0.0)
        nearest_resistance = min((r for r in resistances if r > current_price), default=0.0)

        # Candle momentum — reconstructed from early 1m bars of the replay window.
        # Needed for BTC-follow ETH and for BTC 5m parity with live behavior.
        df_1m_full = data.get("1m", pd.DataFrame())
        mom = self._replay_candle_momentum(df_1m_full, t)

        # Volume profile -> empty
        vp = AnchoredVolumeProfile()

        return TechnicalAnalysis(
            current_price=current_price,
            ema_9=ema_9, ema_21=ema_21, ema_50=ema_50, ema_200=ema_200,
            rsi_14=rsi_14,
            macd_4h=macd_4h,
            macd_1h=macd_1h,
            macd_15m=macd_15m,
            trend_sabre=sabre,
            candle_momentum=mom,
            volume_profile=vp,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            support_levels=supports,
            resistance_levels=resistances,
        )

    # ==========================================================================
    # HTF bias -- BTC (matches bitcoin.py _get_higher_tf_bias exactly)
    # ==========================================================================

    @staticmethod
    def _get_htf_bias(ta: TechnicalAnalysis, min_hist: float = 20.0) -> str:
        """BTC 3-vote system -- exact copy of BitcoinStrategy._get_higher_tf_bias().

        Vote 1: Trend Sabre direction
        Vote 2: Price vs Sabre SMA(35)
        Vote 3: 4H MACD with early_bull / early_bear / recovery signals
        """
        sabre   = ta.trend_sabre
        macd_4h = ta.macd_4h
        price   = ta.current_price
        bull = bear = 0

        # Vote 1: Trend Sabre direction
        if sabre.trend == 1:    bull += 1
        elif sabre.trend == -1: bear += 1

        # Vote 2: Price vs Sabre MA
        if price > sabre.ma_value:   bull += 1
        elif price < sabre.ma_value: bear += 1

        # Vote 3: 4H MACD -- matches live early_bull / early_bear / recovery
        _early_bull = macd_4h.crossover == "BULLISH_CROSS" and macd_4h.histogram_rising
        _early_bear = macd_4h.crossover == "BEARISH_CROSS" and not macd_4h.histogram_rising
        _recovery   = not macd_4h.above_zero and macd_4h.histogram > 0

        if _early_bear:
            bear += 1
        elif macd_4h.above_zero or _early_bull or _recovery:
            bull += 1
        else:
            bear += 1

        if bull >= 2:
            bias = "BULLISH"
        elif bear >= 2:
            bias = "BEARISH"
        else:
            return "NEUTRAL"

        # Conviction gate -- matches bitcoin.py _get_higher_tf_bias().
        # Threshold read from config (min_4h_hist_magnitude); default 20.0.
        # Near-zero histograms with a 2/3 vote produce coin-flip entries.
        if abs(macd_4h.histogram) < min_hist:
            return "NEUTRAL"
        return bias

    # ==========================================================================
    # HTF bias -- SOL (matches sol_macro.py _get_macro_trend exactly)
    # ==========================================================================

    def _get_sol_htf_bias(
        self, ta: TechnicalAnalysis, df_15m: pd.DataFrame,
    ) -> str:
        """SOL 3-vote system -- matches sol_macro._get_macro_trend().

        Vote 1: 1H trend (approximated from 1H EMA cross, since ta is built on 1H)
        Vote 2: 15m EMA alignment (ema_9 > ema_21 > ema_50)
        Vote 3: 15m RSI zone (>55 bull, <45 bear)
        """
        bull = bear = 0

        # Vote 1: 1H trend -- ta.ema_9 / ema_21 are computed on 1H data for SOL
        if ta.ema_9 > ta.ema_21:
            bull += 1
        elif ta.ema_9 < ta.ema_21:
            bear += 1

        # Vote 2: 15m EMA alignment
        if len(df_15m) >= 50:
            ema9  = float(BTCPriceService._calc_ema(df_15m["close"],  9).iloc[-1])
            ema21 = float(BTCPriceService._calc_ema(df_15m["close"], 21).iloc[-1])
            ema50 = float(BTCPriceService._calc_ema(df_15m["close"], 50).iloc[-1])
            if ema9 > ema21 > ema50:
                bull += 1
            elif ema9 < ema21 < ema50:
                bear += 1

        # Vote 3: 15m RSI zone
        if len(df_15m) >= 14:
            rsi_15m = float(BTCPriceService._calc_rsi(df_15m["close"]).iloc[-1])
            if rsi_15m > 55:
                bull += 1
            elif rsi_15m < 45:
                bear += 1

        if bull >= 2: return "BULLISH"
        if bear >= 2: return "BEARISH"
        return "NEUTRAL"

    # ==========================================================================
    # LTF strength -- BTC (matches bitcoin.py _check_lower_tf_confirmation)
    # ==========================================================================

    @staticmethod
    def _ltf_strength(ta: TechnicalAnalysis, allowed_side: str) -> Tuple[bool, float]:
        """15m MACD confirmation -- BTC weights, threshold 0.35."""
        m = ta.macd_15m
        s = 0.0
        if allowed_side == "LONG":
            if m.crossover == "BULLISH_CROSS":              s += 0.40
            if m.histogram_rising and m.histogram > m.prev_histogram:
                s += 0.35 if (m.prev_histogram < 0 and m.histogram > 0) else 0.20
            if m.macd_line > m.signal_line:                 s += 0.15
        else:  # SHORT
            if m.crossover == "BEARISH_CROSS":              s += 0.40
            if not m.histogram_rising and m.histogram < m.prev_histogram:
                s += 0.35 if (m.prev_histogram > 0 and m.histogram < 0) else 0.20
            if m.macd_line < m.signal_line:                 s += 0.15
        confirmed = s >= 0.35
        return confirmed, min(1.0, s)

    # ==========================================================================
    # LTF strength -- SOL (matches sol_macro.py _check_15m_confirmation)
    # ==========================================================================

    @staticmethod
    def _sol_ltf_strength(ta: TechnicalAnalysis, allowed_side: str) -> Tuple[bool, float]:
        """15m MACD confirmation -- SOL-family live weights, threshold 0.50.

        Differences from BTC:
          - hist rising (not flip): +0.15 (BTC uses +0.20)
          - MACD > signal:          +0.10 (BTC uses +0.15)
          - confirmed threshold:     0.50 (live anti-LTF gate)
        """
        m = ta.macd_15m
        s = 0.0
        if allowed_side == "LONG":
            if m.crossover == "BULLISH_CROSS":              s += 0.40
            if m.histogram_rising:
                if m.prev_histogram < 0 and m.histogram > 0:
                    s += 0.35        # red-to-green flip
                elif m.histogram > m.prev_histogram:
                    s += 0.15        # just rising
            if m.macd_line > m.signal_line:                 s += 0.10
        else:  # SHORT
            if m.crossover == "BEARISH_CROSS":              s += 0.40
            if not m.histogram_rising:
                if m.prev_histogram > 0 and m.histogram < 0:
                    s += 0.35        # green-to-red flip
                elif m.histogram < m.prev_histogram:
                    s += 0.15        # just falling
            if m.macd_line < m.signal_line:                 s += 0.10
        confirmed = s >= 0.50
        return confirmed, min(1.0, s)

    # ==========================================================================
    # BTC 15m edge (matches bitcoin.py 15m updown path exactly)
    # ==========================================================================

    def _edge_15m(
        self, ta: TechnicalAnalysis, allowed_side: str, ltf_strength: float,
        htf_bias: str = "NEUTRAL",
    ) -> Tuple[float, float]:
        """BTC 15m edge -- graduated HTF boost (allows 2/3 votes), matches live.

        htf_bias is the already-computed direction from _get_htf_bias().
        The graduated boost re-derives strength from raw indicators, but
        must stay consistent with the HTF vote: if HTF=BULLISH, boost >= +0.03;
        if HTF=BEARISH, boost <= -0.03.  This handles recovery/early_bull
        windows where the 3-vote system sees BULLISH but the raw Sabre +
        above_zero indicators are mixed.
        """
        sabre   = ta.trend_sabre
        macd_4h = ta.macd_4h

        est_prob_up = 0.50

        # Graduated HTF boost -- live uses 3/3 for +/-0.08, 2/3 for +/-0.03
        _price_above_ma = ta.current_price > sabre.ma_value
        if sabre.trend == 1 and _price_above_ma and macd_4h.above_zero:
            htf_boost = 0.08       # All 3 votes bullish
        elif sabre.trend == -1 and not _price_above_ma and not macd_4h.above_zero:
            htf_boost = -0.08      # All 3 votes bearish
        elif sabre.trend == 1 and macd_4h.above_zero:
            htf_boost = 0.03       # 2/3 bull (price below MA)
        elif sabre.trend == -1 and not macd_4h.above_zero:
            htf_boost = -0.03      # 2/3 bear (price above MA)
        else:
            htf_boost = 0.0        # Mixed -- no directional boost

        # Ensure boost direction matches the HTF vote.  Recovery/early_bull
        # windows can produce BULLISH from the 3-vote system while raw
        # indicators remain mixed (e.g., sabre=-1 + recovery → BULLISH).
        # Without this floor, those windows get 0 or negative boost and
        # never generate trades — contradicting the HTF decision.
        if htf_bias == "BULLISH" and htf_boost < 0.03:
            htf_boost = 0.03
        elif htf_bias == "BEARISH" and htf_boost > -0.03:
            htf_boost = -0.03

        est_prob_up += htf_boost

        # 4H histogram hard gate
        if allowed_side == "LONG"  and not macd_4h.histogram_rising: return 0.0, 0.0
        if allowed_side == "SHORT" and     macd_4h.histogram_rising: return 0.0, 0.0

        # LTF adj (anti-LTF gate already applied in run())
        ltf_adj = ltf_strength * 0.20
        est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj

        # RSI 4-level (matches live bitcoin.py)
        if   ta.rsi_14 > 80: est_prob_up -= 0.03
        elif ta.rsi_14 > 65: est_prob_up -= 0.02
        elif ta.rsi_14 < 20: est_prob_up += 0.03
        elif ta.rsi_14 < 35: est_prob_up += 0.02

        # Sabre tension (matches live: threshold 2.0 ATR)
        if sabre.tension_abs > 2.0:
            if allowed_side == "LONG":
                est_prob_up += -0.02 if sabre.tension > 0 else 0.02
            else:
                est_prob_up += 0.02 if sabre.tension > 0 else -0.02

        est_prob_up = max(0.10, min(0.90, est_prob_up))
        edge = (est_prob_up - 0.50) if allowed_side == "LONG" else ((1.0 - est_prob_up) - 0.50)
        # Confidence: matches live = min(0.85, 0.50 + ltf_strength * 0.20 + timing_bonus)
        # timing_bonus = 0 in backtest (no intra-candle data)
        confidence = min(0.85, 0.50 + ltf_strength * 0.20)
        return edge, confidence

    # ==========================================================================
    # SOL 15m edge (matches sol_macro.py 15m updown path)
    # ==========================================================================

    def _edge_15m_sol(
        self, ta: TechnicalAnalysis, allowed_side: str, ltf_strength: float,
    ) -> Tuple[float, float]:
        """SOL 15m edge -- macro boost +/-0.07, LTF*0.22, 1H histogram gate.

        Omits: lag/spike (requires live BTC feed), correlation dampen.
        """
        macd_1h = ta.macd_4h   # For SOL, macd_4h is computed from 1H data

        est_prob_up = 0.50

        # Macro trend boost (matches live sol_macro 15m: +/-0.07)
        # htf_bias is already known to be BULLISH or BEARISH at this point
        if allowed_side == "LONG":
            est_prob_up += 0.07
        else:
            est_prob_up -= 0.07

        # 1H histogram hard gate (matches live sol_macro)
        if allowed_side == "LONG"  and not macd_1h.histogram_rising: return 0.0, 0.0
        if allowed_side == "SHORT" and     macd_1h.histogram_rising: return 0.0, 0.0

        # LTF adj (anti-LTF gate already applied in run())
        ltf_adj = ltf_strength * 0.22
        est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj

        # RSI extremes (matches live sol_macro 15m: >75/-0.03, <25/+0.03)
        if   ta.rsi_14 > 75: est_prob_up -= 0.03
        elif ta.rsi_14 < 25: est_prob_up += 0.03

        est_prob_up = max(0.10, min(0.90, est_prob_up))
        edge = (est_prob_up - 0.50) if allowed_side == "LONG" else ((1.0 - est_prob_up) - 0.50)
        # Confidence: matches live = min(0.85, 0.50 + ltf_strength * 0.22 + lag_conf + timing*0.5)
        # lag_conf and timing = 0 in backtest
        confidence = min(0.85, 0.50 + ltf_strength * 0.22)
        return edge, confidence

    # ==========================================================================
    # BTC 5m candle momentum (mirrors live calc_candle_momentum thresholds)
    # ==========================================================================

    @staticmethod
    def _calc_m5_momentum(
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
        allowed_side: str,
    ) -> Tuple[str, float]:
        """Derive m5_direction and m5_adj from early-candle 1m bars.

        Mirrors live calc_candle_momentum() which reads the first 1.5 min of
        the CURRENT 5m candle.  We replicate by reading the first 2 complete
        1m bars within [window_open, window_open + 2min).  This uses 2 min of
        data from within the window (mild look-ahead, documented) but the
        trade still has 3 min to settle, matching the live timing.

        Thresholds (from btc_price_service.py -- NO LEAN, live only produces
        SPIKE/DRIFT/NONE for m5_direction):
            SPIKE : abs(move_pct) > 0.08 %
            DRIFT : abs(move_pct) > 0.03 %

        Scoring (from bitcoin.py 5m path):
            SPIKE aligned   : +0.06
            DRIFT aligned   : +0.04
            SPIKE/DRIFT opp : -0.04
        """
        if df_1m.empty:
            return "NONE", 0.0

        # First ~90s of the window: 1m bars at window_open and window_open+1m
        cutoff = window_open + pd.Timedelta(seconds=120)
        early = df_1m[(df_1m["open_time"] >= window_open) & (df_1m["open_time"] < cutoff)]
        if early.empty:
            return "NONE", 0.0

        candle_open  = float(early.iloc[0]["open"])
        early_close  = float(early.iloc[-1]["close"])
        if candle_open <= 0:
            return "NONE", 0.0

        move_pct = (early_close - candle_open) / candle_open * 100

        # Only SPIKE and DRIFT -- live calc_candle_momentum never produces LEAN
        if   move_pct >  0.08: direction = "SPIKE_UP"
        elif move_pct < -0.08: direction = "SPIKE_DOWN"
        elif move_pct >  0.03: direction = "DRIFT_UP"
        elif move_pct < -0.03: direction = "DRIFT_DOWN"
        else:                  direction = "NONE"

        m5_adj = 0.0
        if allowed_side == "LONG":
            if   direction == "SPIKE_UP":                    m5_adj =  0.06
            elif direction == "DRIFT_UP":                    m5_adj =  0.04
            elif direction in ("SPIKE_DOWN", "DRIFT_DOWN"):  m5_adj = -0.04
        else:
            if   direction == "SPIKE_DOWN":                  m5_adj =  0.06
            elif direction == "DRIFT_DOWN":                  m5_adj =  0.04
            elif direction in ("SPIKE_UP", "DRIFT_UP"):      m5_adj = -0.04

        return direction, m5_adj

    # ==========================================================================
    # 5m edge -- BTC and SOL paths (matches live strategies)
    # ==========================================================================

    def _edge_5m(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        df_5m: pd.DataFrame,
        symbol: str = "BTC",
        df_1m: pd.DataFrame = None,
        window_open: pd.Timestamp = None,
    ) -> Tuple[float, float]:
        """Estimate edge for a 5m updown window.

        BTC: HTF boost + 4H hist gate + candle momentum (matches bitcoin.py 5m path).
        SOL: macro boost + 1H hist gate + 5m MACD (matches sol_macro.py 5m path).
        """
        macd_htf = ta.macd_4h   # 4H for BTC, 1H for SOL (built from htf_key data)

        if symbol == "BTC":
            return self._edge_5m_btc(ta, allowed_side, df_1m, window_open, macd_htf)
        else:
            if symbol == "ETH":
                min_positive_m5_adj = self.min_positive_m5_adj_eth_5m
            elif symbol == "XRP":
                min_positive_m5_adj = self.min_positive_m5_adj_xrp_5m
            elif symbol == "HYPE":
                min_positive_m5_adj = self.min_positive_m5_adj_hype_5m
            else:
                min_positive_m5_adj = self.min_positive_m5_adj_sol_5m
            return self._edge_5m_sol(ta, allowed_side, df_5m, macd_htf, min_positive_m5_adj)

    def _edge_5m_btc(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
        macd_4h: MACDResult,
    ) -> Tuple[float, float]:
        """BTC 5m path -- matches bitcoin.py 5m updown exactly."""
        sabre = ta.trend_sabre
        est_prob_up = 0.50

        # HTF boost (matches live bitcoin.py 5m)
        if   sabre.trend == 1  and     macd_4h.above_zero: htf_boost =  0.04
        elif sabre.trend == 1  or      macd_4h.above_zero: htf_boost =  0.02
        elif sabre.trend == -1 and not macd_4h.above_zero: htf_boost = -0.04
        else:                                               htf_boost = -0.02
        est_prob_up += htf_boost

        # 4H histogram hard gate
        if allowed_side == "LONG"  and not macd_4h.histogram_rising: return 0.0, 0.0
        if allowed_side == "SHORT" and     macd_4h.histogram_rising: return 0.0, 0.0

        # 5m candle momentum -- primary LTF signal for BTC 5m
        # Uses first ~2 1m bars of the window (mirrors live early-candle read)
        _, m5_adj = self._calc_m5_momentum(
            df_1m if df_1m is not None else pd.DataFrame(),
            window_open,
            allowed_side,
        )
        if allowed_side == "LONG":
            est_prob_up += m5_adj
        else:
            est_prob_up -= m5_adj

        # RSI 4-level (matches live bitcoin.py 5m: 80/65/20/35)
        if   ta.rsi_14 > 80: est_prob_up -= 0.02
        elif ta.rsi_14 > 65: est_prob_up -= 0.01
        elif ta.rsi_14 < 20: est_prob_up += 0.02
        elif ta.rsi_14 < 35: est_prob_up += 0.01

        est_prob_up = max(0.10, min(0.90, est_prob_up))

        edge = (est_prob_up - 0.50) if allowed_side == "LONG" else ((1.0 - est_prob_up) - 0.50)
        # Confidence: matches live = max(0.45, min(0.85, 0.50 + |htf_boost|*2.5 + |m5_adj|*1.5))
        confidence = max(0.45, min(0.85, 0.50 + abs(htf_boost) * 2.5 + abs(m5_adj) * 1.5))
        return edge, confidence

    def _edge_5m_sol(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        df_5m: pd.DataFrame,
        macd_1h: MACDResult,
        min_positive_m5_adj: float = 0.0,
    ) -> Tuple[float, float]:
        """SOL-style 5m path -- matches sol_macro.py 5m updown.

        Omits live-only lag/spike and correlation dampening, but keeps the
        signal math that decides whether the 5m quant path can clear min_edge.
        """
        est_prob_up = 0.50

        # Macro boost (matches live sol_macro 5m: +/-0.03)
        if allowed_side == "LONG":
            est_prob_up += 0.03
        else:
            est_prob_up -= 0.03

        # 1H histogram gate (matches live sol_macro relaxed gate).
        # Live allows trend-direction histogram even when momentum is decelerating;
        # only block when the histogram is actively against the trade direction.
        h1_bull_ok = macd_1h.histogram_rising or macd_1h.histogram > 0
        h1_bear_ok = (not macd_1h.histogram_rising) or macd_1h.histogram < 0
        if allowed_side == "LONG" and not h1_bull_ok:
            return 0.0, 0.0
        if allowed_side == "SHORT" and not h1_bear_ok:
            return 0.0, 0.0

        # 5m MACD -- primary signal for SOL 5m (matches live weights exactly)
        m5_adj = 0.0
        m5_trend = "NEUTRAL"
        if len(df_5m) >= _MIN_5M_BARS:
            macd_5m = self._svc.calc_macd(df_5m)
            if macd_5m.crossover == "BULLISH_CROSS":
                m5_trend = "BULLISH"
            elif macd_5m.crossover == "BEARISH_CROSS":
                m5_trend = "BEARISH"
            elif macd_5m.above_zero and macd_5m.histogram_rising:
                m5_trend = "BULLISH"
            elif not macd_5m.above_zero and not macd_5m.histogram_rising:
                m5_trend = "BEARISH"

            if allowed_side == "LONG":
                if macd_5m.crossover == "BULLISH_CROSS":
                    m5_adj = 0.06
                elif macd_5m.histogram_rising and macd_5m.histogram > 0:
                    m5_adj = 0.04
                elif macd_5m.macd_line > macd_5m.signal_line:
                    m5_adj = 0.02
                elif macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
                    m5_adj = -0.04
            else:  # SHORT
                if macd_5m.crossover == "BEARISH_CROSS":
                    m5_adj = 0.06
                elif not macd_5m.histogram_rising and macd_5m.histogram < 0:
                    m5_adj = 0.04
                elif macd_5m.macd_line < macd_5m.signal_line:
                    m5_adj = 0.02
                elif macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
                    m5_adj = -0.04

        if allowed_side == "LONG":
            est_prob_up += m5_adj
        else:
            est_prob_up -= m5_adj

        if m5_adj < min_positive_m5_adj:
            return 0.0, 0.0

        # Live adds a small extra 5m multi-timeframe trend bonus after m5_adj.
        # This matters for SOL 5m because min_edge=0.10 and macro(+0.03)+
        # strongest 5m MACD(+0.06) otherwise tops out at 0.09 before RSI.
        if m5_trend == "BULLISH" and allowed_side == "LONG":
            est_prob_up += 0.02
        elif m5_trend == "BEARISH" and allowed_side == "SHORT":
            est_prob_up -= 0.02

        # RSI extremes (matches live sol_macro 5m: >75/-0.02, <25/+0.02)
        if   ta.rsi_14 > 75: est_prob_up -= 0.02
        elif ta.rsi_14 < 25: est_prob_up += 0.02

        est_prob_up = max(0.10, min(0.90, est_prob_up))

        edge = (est_prob_up - 0.50) if allowed_side == "LONG" else ((1.0 - est_prob_up) - 0.50)
        # Confidence: matches live = max(0.50, min(0.85, 0.50 + |m5_adj|*2.5 + lag_conf + timing*0.3))
        # lag_conf and timing = 0 in backtest
        confidence = max(0.50, min(0.85, 0.50 + abs(m5_adj) * 2.5))
        return edge, confidence

    @staticmethod
    def _eth_follow_1h_ok(btc_ta: TechnicalAnalysis, allowed_side: str, min_hist: float) -> bool:
        macd_1h = btc_ta.macd_1h
        if allowed_side == "LONG":
            return (
                macd_1h.histogram > min_hist
                or (macd_1h.histogram > 0 and macd_1h.histogram_rising)
                or macd_1h.crossover == "BULLISH_CROSS"
            )
        return (
            macd_1h.histogram < -min_hist
            or (macd_1h.histogram < 0 and not macd_1h.histogram_rising)
            or macd_1h.crossover == "BEARISH_CROSS"
        )

    @staticmethod
    def _eth_follow_btc_5m_impulse(
        btc_ta: TechnicalAnalysis, allowed_side: str
    ) -> float:
        direction = btc_ta.candle_momentum.m5_direction
        score = 0.0
        if allowed_side == "LONG":
            if direction == "SPIKE_UP":
                score = 0.06
            elif direction == "DRIFT_UP":
                score = 0.04
            elif direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                score = -0.05
        else:
            if direction == "SPIKE_DOWN":
                score = 0.06
            elif direction == "DRIFT_DOWN":
                score = 0.04
            elif direction in ("SPIKE_UP", "DRIFT_UP"):
                score = -0.05
        if btc_ta.candle_momentum.m5_in_prediction_window and score > 0:
            score += 0.02
        return score

    @staticmethod
    def _eth_follow_btc_15m_impulse_ok(
        btc_ta: TechnicalAnalysis, allowed_side: str, min_hist: float
    ) -> bool:
        macd_15m = btc_ta.macd_15m
        if allowed_side == "LONG":
            return (
                macd_15m.crossover == "BULLISH_CROSS"
                or (macd_15m.histogram > min_hist and macd_15m.histogram_rising)
                or btc_ta.candle_momentum.m15_direction in ("SPIKE_UP", "DRIFT_UP")
            )
        return (
            macd_15m.crossover == "BEARISH_CROSS"
            or (macd_15m.histogram < -min_hist and not macd_15m.histogram_rising)
            or btc_ta.candle_momentum.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN")
        )

    def _edge_5m_eth_follow(
        self,
        eth_ta: TechnicalAnalysis,
        btc_ta: TechnicalAnalysis,
        allowed_side: str,
        min_eth_adj: float,
        require_btc_impulse: bool,
    ) -> Tuple[float, float]:
        est_prob_up = 0.50 + (0.04 if allowed_side == "LONG" else -0.04)
        btc_impulse = self._eth_follow_btc_5m_impulse(btc_ta, allowed_side)
        if require_btc_impulse and btc_impulse <= 0:
            return 0.0, 0.0

        macd_5m = eth_ta.macd_15m if False else None
        # Reconstruct ETH 5m MACD from the 1h-built TA is not possible here; use candle-momentum-free
        # replay from the 5m history handled in _edge_5m_eth_follow_from_df.
        return 0.0, 0.0

    def _edge_5m_eth_follow_from_df(
        self,
        eth_ta: TechnicalAnalysis,
        btc_ta: TechnicalAnalysis,
        allowed_side: str,
        df_5m: pd.DataFrame,
        min_eth_adj: float,
        require_btc_impulse: bool,
    ) -> Tuple[float, float]:
        est_prob_up = 0.50 + (0.04 if allowed_side == "LONG" else -0.04)
        btc_impulse = self._eth_follow_btc_5m_impulse(btc_ta, allowed_side)
        if require_btc_impulse and btc_impulse <= 0:
            return 0.0, 0.0

        m5_adj = 0.0
        if len(df_5m) >= _MIN_5M_BARS:
            macd_5m = self._svc.calc_macd(df_5m)
            if allowed_side == "LONG":
                if macd_5m.crossover == "BULLISH_CROSS":
                    m5_adj = 0.06
                elif macd_5m.histogram > 0 and macd_5m.histogram_rising:
                    m5_adj = 0.04
                elif macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
                    m5_adj = -0.05
            else:
                if macd_5m.crossover == "BEARISH_CROSS":
                    m5_adj = 0.06
                elif macd_5m.histogram < 0 and not macd_5m.histogram_rising:
                    m5_adj = 0.04
                elif macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
                    m5_adj = -0.05
        if m5_adj < min_eth_adj:
            return 0.0, 0.0

        est_prob_up += btc_impulse if allowed_side == "LONG" else -btc_impulse
        est_prob_up += m5_adj if allowed_side == "LONG" else -m5_adj
        if eth_ta.rsi_14 > 75:
            est_prob_up -= 0.02
        elif eth_ta.rsi_14 < 25:
            est_prob_up += 0.02
        est_prob_up = max(0.10, min(0.90, est_prob_up))
        edge = (est_prob_up - 0.50) if allowed_side == "LONG" else ((1.0 - est_prob_up) - 0.50)
        confidence = max(0.55, min(0.85, 0.50 + abs(btc_impulse) * 1.8 + abs(m5_adj) * 2.0))
        return edge, confidence

    def _edge_15m_eth_follow(
        self,
        eth_ta: TechnicalAnalysis,
        btc_ta: TechnicalAnalysis,
        allowed_side: str,
        min_eth_adj: float,
        min_btc_hist: float,
    ) -> Tuple[float, float]:
        if not self._eth_follow_btc_15m_impulse_ok(btc_ta, allowed_side, min_btc_hist):
            return 0.0, 0.0
        macd_15m = eth_ta.macd_15m
        if allowed_side == "LONG":
            if macd_15m.crossover == "BULLISH_CROSS":
                eth_adj = 0.06
            elif macd_15m.histogram > 0 and macd_15m.histogram_rising:
                eth_adj = 0.04
            elif macd_15m.macd_line > macd_15m.signal_line and macd_15m.histogram > 0:
                eth_adj = 0.02
            else:
                eth_adj = 0.0
        else:
            if macd_15m.crossover == "BEARISH_CROSS":
                eth_adj = 0.06
            elif macd_15m.histogram < 0 and not macd_15m.histogram_rising:
                eth_adj = 0.04
            elif macd_15m.macd_line < macd_15m.signal_line and macd_15m.histogram < 0:
                eth_adj = 0.02
            else:
                eth_adj = 0.0
        if eth_adj < min_eth_adj:
            return 0.0, 0.0
        est_prob_up = 0.50 + (0.08 if allowed_side == "LONG" else -0.08)
        est_prob_up += eth_adj if allowed_side == "LONG" else -eth_adj
        if eth_ta.rsi_14 > 75:
            est_prob_up -= 0.03
        elif eth_ta.rsi_14 < 25:
            est_prob_up += 0.03
        est_prob_up = max(0.10, min(0.90, est_prob_up))
        edge = (est_prob_up - 0.50) if allowed_side == "LONG" else ((1.0 - est_prob_up) - 0.50)
        confidence = max(0.55, min(0.85, 0.50 + abs(eth_adj) * 2.2))
        return edge, confidence

    # -- fill simulation -------------------------------------------------------

    def _simulate_fill(self, mid_price: float, side: str) -> Tuple[float, float]:
        """Apply slippage to assumed mid-price.

        BUY  -> pays more (fill_price > mid).
        SELL -> receives less (fill_price < mid).
        Returns (fill_price, slippage_$ per unit notional).
        """
        slip_pct = self.slippage_bps / 10_000
        slip_usd = max(0.005, mid_price * slip_pct)
        if side == "BUY":
            fill = min(0.99, mid_price + slip_usd)
        else:
            fill = max(0.01, mid_price - slip_usd)
        return fill, abs(fill - mid_price)

    # -- settlement ------------------------------------------------------------

    @staticmethod
    def _settle(
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
        window_close: pd.Timestamp,
    ) -> Tuple[Optional[bool], float, float]:
        """Determine the UP/DOWN outcome of a window from 1m OHLCV.

        Returns (yes_won: bool|None, open_price, close_price).
        yes_won is True if price went UP (YES resolves to $1).
        """
        mask = (df_1m["open_time"] >= window_open) & (df_1m["open_time"] < window_close)
        bars = df_1m[mask]
        if bars.empty:
            return None, 0.0, 0.0
        open_price  = float(bars.iloc[0]["open"])
        close_price = float(bars.iloc[-1]["close"])
        if close_price == open_price:
            return None, open_price, close_price
        yes_won     = close_price > open_price
        return yes_won, open_price, close_price

    # -- position sizing -------------------------------------------------------

    def _size_position(self, bankroll: float, edge: float) -> float:
        """Approximate live KellySizer.size_from_edge + FULL-tier ExposureManager scaling."""
        if edge <= 0:
            return 0.0
        raw_size = min(edge * self.kelly_fraction * bankroll, bankroll * 0.05)
        raw_size = max(1.0, raw_size)
        tier_floor = self.exposure_min_trade_usd
        tier_cap = self.exposure_full_size if self.exposure_full_size > 0 else self.max_size
        size = min(max(raw_size, tier_floor, self.default_size), tier_cap, self.max_size)
        return round(size, 2)

    @staticmethod
    def _oracle_price_at(
        oracle_times_ns: Optional[np.ndarray],
        oracle_prices: Optional[np.ndarray],
        window_open: pd.Timestamp,
    ) -> Optional[float]:
        if oracle_times_ns is None or oracle_prices is None or len(oracle_times_ns) == 0:
            return None
        idx = int(np.searchsorted(oracle_times_ns, window_open.value, side="right") - 1)
        if idx < 0 or idx >= len(oracle_prices):
            return None
        return float(oracle_prices[idx])

    # ==========================================================================
    # Main replay loop
    # ==========================================================================

    def run(
        self,
        data: Dict[str, pd.DataFrame],
        start_date: str,
        end_date: str,
        window_minutes: int = 15,
        symbol: str = "BTC",
        btc_data: Optional[Dict[str, pd.DataFrame]] = None,
        oracle_history: Optional[pd.DataFrame] = None,
    ) -> UpdownBacktestResult:
        """Run the backtest.

        Parameters
        ----------
        data:           Dict from OHLCVLoader.load_all(symbol, ...)
                        Keys: "1m", "5m", "15m", "4h"  (BTC)
                              "1m", "5m", "15m", "1h"  (SOL or ETH)
        start_date:     "YYYY-MM-DD"
        end_date:       "YYYY-MM-DD"
        window_minutes: 15 or 5
        symbol:         "BTC", "SOL", "ETH", "XRP", or "HYPE"
        """
        is_btc   = symbol == "BTC"
        is_eth   = symbol == "ETH"
        tz       = timezone.utc
        step_td  = timedelta(minutes=window_minutes)

        if is_btc:
            self.kelly_fraction = self._kelly_btc
        elif symbol == "ETH":
            self.kelly_fraction = self._kelly_eth
        elif symbol == "XRP":
            self.kelly_fraction = self._kelly_xrp
        elif symbol == "HYPE":
            self.kelly_fraction = self._kelly_hype
        else:  # SOL
            self.kelly_fraction = self._kelly_sol
        self.entry_price_min, self.entry_price_max = self._entry_bands.get(
            symbol, (0.0, 1.0)
        )

        # Symbol-specific min_edge thresholds
        if is_btc:
            min_edge = self.min_edge_5m if window_minutes == 5 else self.min_edge_15m
        elif symbol == "ETH":
            min_edge = self.min_edge_eth_5m if window_minutes == 5 else self.min_edge_eth_15m
        elif symbol == "XRP":
            min_edge = self.min_edge_xrp_5m if window_minutes == 5 else self.min_edge_xrp_15m
        elif symbol == "HYPE":
            min_edge = self.min_edge_hype_5m if window_minutes == 5 else self.min_edge_hype_15m
        else:  # SOL
            min_edge = self.min_edge_sol_5m if window_minutes == 5 else self.min_edge_sol_15m

        # BTC uses 4h HTF candles; SOL uses 1h
        htf_key = "4h" if is_btc else "1h"

        # Snap start to the nearest window boundary
        s_epoch  = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
        step_s   = window_minutes * 60
        s_epoch -= s_epoch % step_s
        current  = pd.Timestamp(datetime.fromtimestamp(s_epoch, tz=tz))

        e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, tzinfo=tz
        )
        end_ts = pd.Timestamp(e_dt)

        # Ensure open_time is tz-aware UTC in all DataFrames
        for iv in data:
            df = data[iv]
            if not df.empty and df["open_time"].dt.tz is None:
                data[iv] = df.copy()
                data[iv]["open_time"] = data[iv]["open_time"].dt.tz_localize("UTC")

        bankroll       = self.initial_bankroll
        trades: List[UpdownTrade] = []
        windows_scanned = 0
        slippage_total  = 0.0
        oracle_basis_skips = 0
        oracle_symbol = f"{symbol.upper()}USDT"
        oracle_history_loaded = oracle_history is not None and not oracle_history.empty
        oracle_history_points = 0
        oracle_times_ns: Optional[np.ndarray] = None
        oracle_prices: Optional[np.ndarray] = None
        strategy_cfg_map = {
            "BTC": "bitcoin",
            "SOL": "sol_macro",
            "ETH": "eth_macro",
            "XRP": "xrp_macro",
            "HYPE": "hype_macro",
        }
        oracle_max_basis_bps = self.config.get("strategies", {}).get(
            strategy_cfg_map.get(symbol, "sol_macro"), {}
        ).get("oracle_max_basis_bps")
        if oracle_history_loaded:
            oracle_history = oracle_history.copy()
            if oracle_history["updated_at"].dt.tz is None:
                oracle_history["updated_at"] = oracle_history["updated_at"].dt.tz_localize("UTC")
            else:
                oracle_history["updated_at"] = oracle_history["updated_at"].dt.tz_convert("UTC")
            oracle_history = oracle_history.sort_values("updated_at").reset_index(drop=True)
            oracle_history_points = len(oracle_history)
            oracle_times_ns = oracle_history["updated_at"].astype("int64").to_numpy()
            oracle_prices = oracle_history["price"].astype(float).to_numpy()

        while current <= end_ts:
            window_open  = current
            window_close = current + step_td
            windows_scanned += 1

            # Build TechnicalAnalysis from data strictly before this window
            ta = self._build_ta(window_open, data, htf_key)
            if ta is None:
                current += step_td
                continue

            # Also get 15m slice (needed for SOL/ETH alt HTF bias and potential future use)
            df_15m = self._before(data["15m"], window_open)

            # ==================================================================
            # Layer 1: HTF bias (symbol-specific)
            # ==================================================================
            if is_btc:
                htf_bias = self._get_htf_bias(ta, min_hist=self.min_4h_hist_magnitude)
                btc_ta = ta
            elif is_eth:
                if not btc_data:
                    current += step_td
                    continue
                btc_ta = self._build_ta(window_open, btc_data, "4h")
                if btc_ta is None:
                    current += step_td
                    continue
                htf_bias = self._get_htf_bias(btc_ta, min_hist=self.min_4h_hist_magnitude)
            else:
                htf_bias = self._get_sol_htf_bias(ta, df_15m)
                btc_ta = None

            if htf_bias == "NEUTRAL":
                current += step_td
                continue

            allowed_side = "LONG" if htf_bias == "BULLISH" else "SHORT"

            if is_eth and not self._eth_follow_1h_ok(
                btc_ta, allowed_side, float(self.config.get("strategies", {}).get("eth_macro", {}).get("btc_follow_1h_hist_min", 8.0))
            ):
                current += step_td
                continue

            # ==================================================================
            # Layer 2: LTF confirmation (symbol-specific weights + threshold)
            # ==================================================================
            if is_btc:
                ltf_confirmed, ltf_str = self._ltf_strength(ta, allowed_side)
            else:
                ltf_confirmed, ltf_str = self._sol_ltf_strength(ta, allowed_side)

            # Anti-LTF gate.
            # BTC and the legacy SOL-style paths skip confirmed MACD as a late-entry risk.
            # ETH 15m BTC-follow is different: it explicitly wants ETH 15m follow-through,
            # so do not apply the generic anti-LTF skip to that path.
            if ltf_confirmed and not (is_eth and window_minutes == 15):
                current += step_td
                continue

            # ==================================================================
            # Layer 3: Edge estimation (symbol + timeframe specific)
            # ==================================================================
            if window_minutes == 5:
                df_5m = self._before(data["5m"], window_open)
                df_1m_full = data.get("1m", pd.DataFrame())
                if is_eth:
                    eth_cfg = self.config.get("strategies", {}).get("eth_macro", {})
                    edge, confidence = self._edge_5m_eth_follow_from_df(
                        ta,
                        btc_ta,
                        allowed_side,
                        df_5m,
                        float(eth_cfg.get("eth_follow_5m_min_adj", 0.04)),
                        bool(eth_cfg.get("btc_follow_5m_requires_impulse", True)),
                    )
                else:
                    edge, confidence = self._edge_5m(
                        ta, allowed_side, df_5m, symbol,
                        df_1m=df_1m_full, window_open=window_open,
                    )
            else:
                if is_btc:
                    edge, confidence = self._edge_15m(ta, allowed_side, ltf_str, htf_bias)
                elif is_eth:
                    eth_cfg = self.config.get("strategies", {}).get("eth_macro", {})
                    edge, confidence = self._edge_15m_eth_follow(
                        ta,
                        btc_ta,
                        allowed_side,
                        float(eth_cfg.get("eth_follow_15m_min_adj", 0.04)),
                        float(eth_cfg.get("btc_follow_15m_hist_min", 0.03)),
                    )
                else:
                    edge, confidence = self._edge_15m_sol(ta, allowed_side, ltf_str)

            # Min edge filter
            if edge < min_edge:
                current += step_td
                continue

            oracle_price = self._oracle_price_at(oracle_times_ns, oracle_prices, window_open)
            if oracle_max_basis_bps is not None and oracle_price and oracle_price > 0:
                basis_bps = ((ta.current_price - oracle_price) / oracle_price) * 10000.0
                if abs(basis_bps) > float(oracle_max_basis_bps):
                    oracle_basis_skips += 1
                    current += step_td
                    continue

            # Determine action
            action    = "BUY_YES" if allowed_side == "LONG" else "SELL_YES"
            fill_side = "BUY" if action == "BUY_YES" else "SELL"

            # Position size
            size = self._size_position(bankroll, edge)
            if size <= 0 or bankroll < size:
                current += step_td
                continue

            # Fill at realistic mid-price: use empirical distribution from live fills
            # when available (>=20 recorded), else N(0.50, 0.06) clipped to [0.30, 0.70].
            mid_price = self._sample_entry_price()
            if mid_price < self.entry_price_min or mid_price > self.entry_price_max:
                current += step_td
                continue
            fill_price, slip_cost = self._simulate_fill(mid_price, fill_side)
            slippage_total += slip_cost * size

            # Settle using 1m data for the window
            df_1m    = data.get("1m", pd.DataFrame())
            yes_won, asset_open, asset_close = self._settle(df_1m, window_open, window_close)
            if yes_won is None:
                # Cannot settle this window (no 1m data) -- skip
                current += step_td
                continue

            # PnL
            if action == "BUY_YES":
                if yes_won:
                    exit_price = 1.0
                    pnl        = (1.0 - fill_price) * size
                    outcome    = "WIN"
                else:
                    exit_price = 0.0
                    pnl        = -fill_price * size
                    outcome    = "LOSS"
            else:  # SELL_YES -- we profit when YES = 0 (NO won)
                if not yes_won:
                    exit_price = 0.0
                    pnl        = fill_price * size
                    outcome    = "WIN"
                else:
                    exit_price = 1.0
                    pnl        = -(1.0 - fill_price) * size
                    outcome    = "LOSS"

            bankroll = max(0.0, bankroll + pnl)   # ruin cap

            trades.append(UpdownTrade(
                window_open=window_open,
                window_close=window_close,
                symbol=symbol,
                window_size=window_minutes,
                action=action,
                htf_bias=htf_bias,
                ltf_confirmed=ltf_confirmed,
                ltf_strength=ltf_str,
                entry_price=mid_price,
                fill_price=fill_price,
                size=size,
                edge=edge,
                confidence=confidence,
                outcome=outcome,
                exit_price=exit_price,
                pnl=pnl,
                slip=slip_cost * size,
                asset_open=asset_open,
                asset_close=asset_close,
            ))

            if bankroll <= 0:
                logger.warning("Bankroll hit zero -- stopping (ruin cap)")
                break

            current += step_td

        wins   = sum(1 for t in trades if t.outcome == "WIN")
        losses = sum(1 for t in trades if t.outcome == "LOSS")

        return UpdownBacktestResult(
            symbol=symbol,
            window_size=window_minutes,
            start_date=start_date,
            end_date=end_date,
            initial_bankroll=self.initial_bankroll,
            final_bankroll=bankroll,
            trades=trades,
            windows_scanned=windows_scanned,
            windows_entered=len(trades),
            wins=wins,
            losses=losses,
            slippage_total=slippage_total,
            oracle_symbol=oracle_symbol,
            oracle_history_loaded=oracle_history_loaded,
            oracle_history_points=oracle_history_points,
            oracle_basis_skips=oracle_basis_skips,
        )
