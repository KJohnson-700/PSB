"""
Updown Backtest Engine — replays the Bitcoin / SOL / ETH Up-or-Down strategies
against historical Binance OHLCV using the EXACT same indicator math as
the live strategies.

Architecture
────────────
1.  Caller pre-fetches OHLCV via OHLCVLoader. Optional Polymarket YES 1m marks
    (``backtest.polymarket_marks`` + ``POLYMARKETDATA_API_KEY``) are loaded per
    window when enabled; otherwise only OHLCV is used.
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
7.  Exit handling: optional **Polymarket YES 1m** marks (``backtest.polymarket_marks`` +
    ``POLYMARKETDATA_API_KEY``) for exit replay; else shared exit rules on the
    synthetic underlying proxy (see ``updown_exit_shared`` / ``updown_polymarket_marks``).
8.  Ruin cap enforced: ``bankroll = max(0, bankroll + pnl)``.

Signal fidelity
───────────────
BTC: mirrors bitcoin.py exactly (HTF 3-vote with early_bull/early_bear/
     recovery, graduated 15m boost, anti-LTF gate, 5m candle momentum).
Non-BTC crypto: uses each alt's own 1H/15m/5m indicators as the primary
     direction source; BTC remains secondary context/follow/correlation input.
     Lag/correlation signals are omitted (require live BTC feed).
     15m IQL + optional stricter 5m SELL gates (min m5_adj + min BTC-alt 1h
     correlation via aligned 1m bars) mirror live config when enabled.

Checklist (from docs/BACKTEST.md)
──────────────────────────────────
[x] Ruin cap
[x] Slippage modeled (entry + exit at settlement is 0/1, no exit slip)
[x] Resolution settlement applied (actual OHLCV direction)
[x] Universe pinned (exact date range + symbol logged in result)
[x] Timestamp alignment: all slices use open_time < T (strict)
[x] Exit strategy: Polymarket YES 1m (optional) or synthetic proxy + shared exit rules
"""
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.analysis.kelly_sizer import KellySizer
from src.analysis.lane_entry_policy import LaneEntryPolicy, resolve_lane_entry_policy
from src.analysis.btc_price_service import (
    BTCPriceService,
    TechnicalAnalysis,
    MACDResult,
    TrendSabreResult,
    CandleMomentum,
    AnchoredVolumeProfile,
)
from src.backtest.updown_polymarket_marks import try_load_yes_series_for_window
from src.analysis.lane_calibration_replay import (
    build_lane_calibrator_for_replay,
    edge_from_raw_est_prob,
    record_updown_calibration_close,
)
from src.strategies.btc_updown_5m import (
    compute_btc_5m_quant,
    m5_candle_age_minutes,
    m5_in_prediction_window_at_age,
    score_m5_direction,
)
from src.execution.performance_feedback import get_drift_min_edge_mult
from src.analysis.lane_calibration_replay import (
    build_lane_calibrator_for_replay,
    edge_from_raw_est_prob,
    record_updown_calibration_close,
)
from src.strategies.btc_updown_5m import (
    compute_btc_5m_quant,
    m5_candle_age_minutes,
    m5_in_prediction_window_at_age,
    score_m5_direction,
)
from src.execution.updown_exit_shared import (
    adverse_for_updown_cents_time_stop,
    cents_stop_for_entry_price,
    effective_updown_stop_loss_pct,
    parse_updown_exit_globals,
    resolve_updown_exit_params_for_position,
    scaled_exit_window_mins,
)

logger = logging.getLogger(__name__)

# Minimum bars required before indicators are reliable
_MIN_4H_BARS  = 65   # Sabre SMA(35) + ATR(14) + warmup
_MIN_15M_BARS = 50   # MACD(26,9) + warmup
_MIN_5M_BARS  = 40   # 5m MACD warmup


def replay_window_tf_label(window_minutes: int) -> str:
    """Config / lane key for replay (live uses 1h, not deprecated 30m)."""
    wm = int(window_minutes)
    if wm == 5:
        return "5m"
    if wm >= 45:
        return "1h"
    return "15m"


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
    action:        str           # "BUY_YES" or "BUY_NO"
    htf_bias:      str           # "BULLISH" | "BEARISH"
    ltf_confirmed: bool
    ltf_strength:  float
    entry_price:   float         # Mid YES before slip (reference for both legs)
    fill_price:    float         # After slip: YES fill for BUY_YES, NO fill for BUY_NO
    size:          float         # $ notional
    edge:          float         # Entry edge vs market YES (est_prob_up - yes for LONG)
    confidence:    float
    outcome:       Optional[str] = None   # "WIN" | "LOSS"
    exit_price:    float = 0.0
    pnl:           float = 0.0
    slip:          float = 0.0   # Slippage cost in $ for this trade
    asset_open:    float = 0.0   # BTC / SOL / ETH price at window open
    asset_close:   float = 0.0   # BTC / SOL / ETH price at window close
    exit_reason:   str = "settlement"
    raw_est_prob_up: float = 0.0
    lane_id:       str = ""


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
    replay_assumptions: Dict[str, Any] = field(default_factory=dict)
    skip_counts: Dict[str, int] = field(default_factory=dict)
    replay_assumptions: Dict[str, Any] = field(default_factory=dict)
    total_windows: int = 0
    run_complete: bool = True
    elapsed_seconds: float = 0.0

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
                windows_scanned=UpdownBacktestResult._count_windows_for_range(
                    start, end, self.window_size
                ),
                windows_entered=len(trades),
                wins=wins,
                losses=losses,
                slippage_total=round(slip, 4),
                oracle_symbol=self.oracle_symbol,
                oracle_history_loaded=self.oracle_history_loaded,
                oracle_history_points=self.oracle_history_points,
                replay_assumptions=dict(self.replay_assumptions or {}),
                skip_counts={},
                total_windows=UpdownBacktestResult._count_windows_for_range(start, end, self.window_size),
                run_complete=self.run_complete,
                elapsed_seconds=self.elapsed_seconds,
            )

        return _build(train_trades, self.start_date, test_start), \
               _build(test_trades,  test_start,      self.end_date)


# ==============================================================================
# Engine
# ==============================================================================


@dataclass
class ReplayDirectionDecision:
    """Replay-time direction decision for alt macro strategies."""

    alt_htf_bias: str = "NEUTRAL"
    primary_htf_bias: str = "NEUTRAL"
    allowed_side: Optional[str] = None
    side_source: str = ""
    skip_reason: str = ""
    lag_magnitude: Optional[float] = None
    skip_btc_follow_1h: bool = False


class UpdownBacktestEngine:
    """Replays Bitcoin or alt-coin (SOL / ETH) updown strategy on historical OHLCV.

    Does NOT make any live API calls during the replay -- all data is
    pre-fetched by the caller via OHLCVLoader and passed into ``run()``.
    """

    def __init__(self, config: Dict[str, Any], initial_bankroll: float = 500.0):
        self.config           = config
        self.initial_bankroll = initial_bankroll
        self._replay_min_edge_sources: Dict[str, str] = {}

        # Slippage config
        slip_cfg         = config.get("backtest", {}).get("slippage", {})
        self.slippage_bps = slip_cfg.get("default_bps", 25)

        bt_cfg   = config.get("backtest", {})
        strat    = config.get("strategies", {})
        btc_cfg  = strat.get("bitcoin",  {})
        sol_cfg  = strat.get("sol_macro",  {})
        eth_cfg  = strat.get("eth_macro",  {})
        xrp_cfg  = strat.get("xrp_macro",  {})
        hype_cfg = strat.get("hype_macro", {})

        self.min_edge_15m       = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=btc_cfg,
            override_key="min_edge_btc_15m",
            live_key="min_edge",
            fallback=0.06,
            label="BTC_15m",
        )
        self.min_edge_5m        = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=btc_cfg,
            override_key="min_edge_btc_5m",
            live_key="min_edge_5m",
            fallback=self.min_edge_15m,
            label="BTC_5m",
        )
        self.min_edge_sol_15m   = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=sol_cfg,
            override_key="min_edge_sol_15m",
            live_key="min_edge",
            fallback=0.06,
            label="SOL_15m",
        )
        self.min_edge_sol_5m    = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=sol_cfg,
            override_key="min_edge_sol_5m",
            live_key="min_edge_5m",
            fallback=self.min_edge_sol_15m,
            label="SOL_5m",
        )
        self.min_edge_eth_15m   = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=eth_cfg,
            override_key="min_edge_eth_15m",
            live_key="min_edge",
            fallback=self.min_edge_sol_15m,
            label="ETH_15m",
        )
        self.min_edge_eth_5m    = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=eth_cfg,
            override_key="min_edge_eth_5m",
            live_key="min_edge_5m",
            fallback=self.min_edge_eth_15m,
            label="ETH_5m",
        )
        self.min_edge_xrp_15m   = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=xrp_cfg,
            override_key="min_edge_xrp_15m",
            live_key="min_edge",
            fallback=self.min_edge_sol_15m,
            label="XRP_15m",
        )
        self.min_edge_xrp_5m    = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=xrp_cfg,
            override_key="min_edge_xrp_5m",
            live_key="min_edge_5m",
            fallback=self.min_edge_xrp_15m,
            label="XRP_5m",
        )
        self.min_edge_hype_15m  = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=hype_cfg,
            override_key="min_edge_hype_15m",
            live_key="min_edge",
            fallback=self.min_edge_sol_15m,
            label="HYPE_15m",
        )
        self.min_edge_hype_5m   = self._resolve_replay_min_edge(
            backtest_cfg=bt_cfg,
            strategy_cfg=hype_cfg,
            override_key="min_edge_hype_5m",
            live_key="min_edge_5m",
            fallback=self.min_edge_hype_15m,
            label="HYPE_5m",
        )
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
        self.min_positive_m5_adj_sol_5m_sell = float(
            sol_cfg.get("min_positive_m5_adj_5m_sell", self.min_positive_m5_adj_sol_5m)
        )
        self.min_positive_m5_adj_eth_5m_sell = float(
            eth_cfg.get("min_positive_m5_adj_5m_sell", self.min_positive_m5_adj_eth_5m)
        )
        self.min_positive_m5_adj_xrp_5m_sell = float(
            xrp_cfg.get("min_positive_m5_adj_5m_sell", self.min_positive_m5_adj_xrp_5m)
        )
        self.min_positive_m5_adj_hype_5m_sell = float(
            hype_cfg.get("min_positive_m5_adj_5m_sell", self.min_positive_m5_adj_hype_5m)
        )
        self.sell_5m_min_corr_sol = float(sol_cfg.get("sell_5m_min_corr", -1.0))
        self.sell_5m_min_corr_eth = float(eth_cfg.get("sell_5m_min_corr", -1.0))
        self.sell_5m_min_corr_xrp = float(xrp_cfg.get("sell_5m_min_corr", -1.0))
        self.sell_5m_min_corr_hype = float(hype_cfg.get("sell_5m_min_corr", -1.0))
        self.eth_btc_follow_1h_required = bool(eth_cfg.get("btc_follow_1h_required", True))

        trade_cfg         = config.get("trading", {})
        self.default_size  = trade_cfg.get("default_position_size", 10.0)
        self.max_size      = trade_cfg.get("max_position_size", 15.0)
        exit_cfg = trade_cfg.get("exit_rules", {})
        self._ude = parse_updown_exit_globals(exit_cfg)
        self.take_profit_pct = self._ude.take_profit_pct
        self.updown_stop_loss_pct = self._ude.updown_stop_loss_pct
        self.updown_stop_cents = self._ude.updown_stop_cents
        self.updown_exit_window_mins = self._ude.updown_exit_window_mins
        self.updown_max_hold_mins = self._ude.updown_max_hold_mins
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

        _pm = config.get("backtest", {}).get("polymarket_marks", {}) or {}
        self._pm_marks_enabled = bool(_pm.get("enabled", False))
        _repo_root = Path(__file__).resolve().parent.parent.parent
        self._pm_marks_cache_root = _repo_root / str(
            _pm.get("cache_dir", "data/backtest/polymarket_marks")
        )
        # Default delay; per-run ``run()`` overwrites from active strategy block.
        self._entry_eval_delay_sec = float(bt_cfg.get("entry_eval_delay_sec", 0.0) or 0.0)
        self.kelly_sizer = KellySizer(config)
        self._lane_calibrator = build_lane_calibrator_for_replay(config)
        self._signal_strategy_name = "bitcoin"
        self._active_strategy_cfg: Dict[str, Any] = {}

    def _resolve_replay_min_edge(
        self,
        *,
        backtest_cfg: Dict[str, Any],
        strategy_cfg: Dict[str, Any],
        override_key: str,
        live_key: str,
        fallback: float,
        label: str,
    ) -> float:
        """Prefer explicit backtest overrides, else mirror the live strategy key."""
        if override_key in backtest_cfg and backtest_cfg.get(override_key) is not None:
            value = float(backtest_cfg.get(override_key))
            self._replay_min_edge_sources[label] = f"backtest.{override_key}"
            return value
        if live_key in strategy_cfg and strategy_cfg.get(live_key) is not None:
            value = float(strategy_cfg.get(live_key))
            self._replay_min_edge_sources[label] = (
                f"strategies.{self._strategy_config_name_for_label(label)}.{live_key}"
            )
            return value
        self._replay_min_edge_sources[label] = f"fallback:{fallback:.4f}"
        return float(fallback)

    @staticmethod
    def _strategy_config_name_for_label(label: str) -> str:
        symbol = label.split("_", 1)[0]
        return {
            "BTC": "bitcoin",
            "SOL": "sol_macro",
            "ETH": "eth_macro",
            "XRP": "xrp_macro",
            "HYPE": "hype_macro",
        }.get(symbol, "sol_macro")

    def _build_replay_assumptions(self, symbol: str, window_minutes: int) -> Dict[str, Any]:
        tf_label = replay_window_tf_label(window_minutes)
        if self._fill_prices is not None:
            entry_source = "empirical_live_fill_distribution"
            entry_points = int(len(self._fill_prices))
        else:
            entry_source = "normal_fallback"
            entry_points = 0
        return {
            "min_edge_source": self._replay_min_edge_sources.get(f"{symbol}_{tf_label}", "unknown"),
            "entry_price_source": entry_source,
            "entry_price_points": entry_points,
            "exit_mark_source": "polymarket_yes_1m" if self._pm_marks_enabled else "underlying_proxy",
            "entry_eval_delay_sec": float(self._entry_eval_delay_sec),
            "polymarket_marks_enabled": bool(self._pm_marks_enabled),
            "liquidity_filter_mode": "not_modeled_in_replay",
            "spread_filter_mode": "not_modeled_in_replay",
        }

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

    def _resolve_entry_window_bounds(
        self, *, tf: str, default_min: float, default_max: float
    ) -> tuple[float, float]:
        """Mirror live entry-window bounds, including optional auto-alignment."""
        if tf not in ("5m", "15m", "1h"):
            tf = "15m"
        cfg = self._active_strategy_cfg or {}
        win_min = float(cfg.get(f"entry_window_{tf}_min", self.config.get(f"entry_window_{tf}_min", default_min)))
        win_max = float(cfg.get(f"entry_window_{tf}_max", self.config.get(f"entry_window_{tf}_max", default_max)))
        if win_min > win_max:
            win_min, win_max = win_max, win_min

        auto_align = cfg.get("entry_window_auto_align", self.config.get("entry_window_auto_align", False))
        if not auto_align:
            return win_min, win_max

        scan_interval_sec = float(self.config.get("entry_window_align_scan_interval_sec", 300))
        if tf == "5m":
            default_expand = 1.0
        elif tf == "1h":
            default_expand = 5.0
        else:
            default_expand = 1.5
        max_expand_min = float(
            self.config.get("entry_window_auto_align_max_expand_min", default_expand)
        )
        jitter_sec = float(self.config.get("entry_window_auto_align_jitter_sec", 15))
        cadence_half_min = scan_interval_sec / 120.0
        expansion_min = max(cadence_half_min, max_expand_min) + max(0.0, jitter_sec) / 60.0

        aligned_min = max(0.0, win_min - expansion_min)
        expanded_upper = win_max + expansion_min
        hard_cap = float(self.config.get("entry_window_hard_cap_mins_left", 0.0) or 0.0)
        aligned_max = min(expanded_upper, hard_cap) if hard_cap > 0 else expanded_upper
        if aligned_max <= aligned_min:
            return win_min, win_max
        return aligned_min, aligned_max

    def _resolve_entry_timing_window_bounds(self, *, tf: str) -> tuple[float, float]:
        if tf not in ("5m", "15m", "1h"):
            tf = "15m"
        presets = {"5m": (1.5, 2.5), "15m": (8.0, 13.0), "1h": (5.0, 55.0)}
        default_min, default_max = presets[tf]
        cfg = self._active_strategy_cfg or {}
        win_min = float(
            cfg.get(
                f"entry_timing_window_{tf}_min",
                self.config.get(f"entry_timing_window_{tf}_min", cfg.get(f"ai_entry_window_{tf}_min", default_min)),
            )
        )
        win_max = float(
            cfg.get(
                f"entry_timing_window_{tf}_max",
                self.config.get(f"entry_timing_window_{tf}_max", cfg.get(f"ai_entry_window_{tf}_max", default_max)),
            )
        )
        if win_min > win_max:
            win_min, win_max = win_max, win_min
        return win_min, win_max

    def _within_entry_timing_window(self, *, mins_left: float, tf: str) -> bool:
        win_min, win_max = self._resolve_entry_timing_window_bounds(tf=tf)
        return win_min <= mins_left <= win_max

    def _entry_timing_window_is_configured(
        self, *, tf: str, strategy_cfg: Dict[str, Any]
    ) -> bool:
        if tf not in ("5m", "15m", "1h"):
            tf = "15m"
        keys = (
            f"entry_timing_window_{tf}_min",
            f"entry_timing_window_{tf}_max",
            f"ai_entry_window_{tf}_min",
            f"ai_entry_window_{tf}_max",
        )
        return any(k in strategy_cfg or k in self.config for k in keys)

    @staticmethod
    def _resolve_entry_eval_delay_sec(
        config: Dict[str, Any], strategy_cfg: Dict[str, Any]
    ) -> float:
        """Scan cadence + latency buffer (live parity), preferring strategy block keys."""
        bt_cfg = config.get("backtest", {}) or {}
        override = bt_cfg.get("entry_eval_delay_sec")
        if override is not None:
            return float(override)
        scan_sec = float(
            strategy_cfg.get("entry_window_align_scan_interval_sec", 0.0) or 0.0
        )
        latency_sec = float(
            strategy_cfg.get("entry_window_latency_buffer_sec", 0.0) or 0.0
        )
        if scan_sec <= 0 and latency_sec <= 0:
            scan_sec = float(
                config.get("entry_window_align_scan_interval_sec", 0.0) or 0.0
            )
            latency_sec = float(
                config.get("entry_window_latency_buffer_sec", 0.0) or 0.0
            )
        return scan_sec + latency_sec

    def _evaluation_minutes_left_at_open(
        self, window_minutes: int, strategy_cfg: Optional[Dict[str, Any]] = None
    ) -> float:
        cfg = strategy_cfg if strategy_cfg is not None else (self._active_strategy_cfg or {})
        delay = self._resolve_entry_eval_delay_sec(self.config, cfg)
        return max(0.0, float(window_minutes) - (delay / 60.0))

    def _evaluation_minutes_left(self, window_minutes: int) -> float:
        return self._evaluation_minutes_left_at_open(window_minutes)

    def _replay_eval_minutes_left(
        self,
        *,
        window_minutes: int,
        lane_policy: LaneEntryPolicy,
        strategy_cfg: Dict[str, Any],
    ) -> float:
        """Minutes left at replay entry decision — inside lane band when open is too early.

        Live scans repeatedly while ``mins_left`` moves down; a one-shot check at window
        open (~30m left on a 30m or 1h market) misses bands like 25–29m and yields zero entries.
        """
        open_eval = self._evaluation_minutes_left_at_open(window_minutes, strategy_cfg)
        win_min = float(lane_policy.entry_window_min)
        win_max = float(lane_policy.entry_window_max)
        if win_max <= win_min or win_max <= 0:
            return open_eval
        if win_min <= open_eval <= win_max:
            return open_eval
        scan_sec = float(
            strategy_cfg.get("entry_window_align_scan_interval_sec", 0.0) or 0.0
        )
        if scan_sec <= 0:
            scan_sec = float(
                self.config.get("entry_window_align_scan_interval_sec", 0.0) or 60.0
            )
        wm = float(window_minutes)
        if win_min > wm:
            return self._evaluation_minutes_left_at_open(window_minutes, strategy_cfg)
        candidate = min(wm, win_max - (scan_sec / 60.0))
        if win_min <= candidate <= win_max:
            return candidate
        mid = (win_min + win_max) / 2.0
        return max(win_min, min(win_max, min(wm, mid)))

    def _legacy_lane_entry_policy_for_replay(
        self,
        *,
        symbol: str,
        strategy_cfg: Dict[str, Any],
        window_minutes: int,
        action: str,
        min_edge: float,
    ) -> Dict[str, Any]:
        tf_label = replay_window_tf_label(window_minutes)
        if window_minutes == 5:
            default_min, default_max = (0.5, 5.0)
        elif window_minutes >= 45:
            default_min, default_max = (1.0, 59.0)
        else:
            default_min, default_max = (2.0, 16.0)
        win_min, win_max = self._resolve_entry_window_bounds(
            tf=tf_label,
            default_min=default_min,
            default_max=default_max,
        )
        entry_price_min, entry_price_max = self._entry_bands.get(symbol, (0.0, 1.0))
        if symbol != "BTC" and action == "BUY_YES" and window_minutes != 5:
            entry_price_max = float(
                strategy_cfg.get(
                    "entry_price_max_15m_yes_side",
                    strategy_cfg.get("entry_price_max_15m_buy_yes", entry_price_max),
                )
            )
        size_multiplier = 1.0
        if window_minutes == 5:
            size_multiplier = float(
                strategy_cfg.get("calibration_size_multiplier_5m", 1.0) or 1.0
            )
        policy_min_edge = float(min_edge)
        if action == "BUY_NO":
            policy_min_edge = max(
                policy_min_edge,
                float(strategy_cfg.get("min_edge_buy_no", 0.0) or 0.0),
            )
        return {
            "enabled": True,
            "min_edge": policy_min_edge,
            "hard_min_edge": float(strategy_cfg.get("hard_min_edge", 0.0) or 0.0),
            "ai_override_min_edge": float(
                strategy_cfg.get("min_edge_5m_ai_override", 0.0) or 0.0
            ),
            "entry_price_min": float(entry_price_min),
            "entry_price_max": float(entry_price_max),
            "entry_window_min": float(win_min),
            "entry_window_max": float(win_max),
            "size_multiplier": float(size_multiplier),
        }

    def _resolve_replay_lane_entry_policy(
        self,
        *,
        symbol: str,
        strategy_name: str,
        strategy_cfg: Dict[str, Any],
        window_minutes: int,
        action: str,
        min_edge: float,
    ):
        side = "up" if action == "BUY_YES" else "down"
        return resolve_lane_entry_policy(
            strategy_name=strategy_name,
            window_size=replay_window_tf_label(window_minutes),
            side=side,
            full_config=self.config,
            legacy_policy=self._legacy_lane_entry_policy_for_replay(
                symbol=symbol,
                strategy_cfg=strategy_cfg,
                window_minutes=window_minutes,
                action=action,
                min_edge=min_edge,
            ),
        )

    def _yes_mid_at_eval(
        self,
        *,
        window_open: pd.Timestamp,
        window_close: pd.Timestamp,
        window_minutes: int,
        df_1m: pd.DataFrame,
        pm_yes: Optional[pd.Series],
        eval_minutes_left: Optional[float] = None,
    ) -> float:
        """YES mid at entry evaluation: Polymarket 1m marks if present, else OHLCV proxy.

        Matches live semantics for edge = est_prob_up - yes (LONG) / yes - est_prob_up (SHORT).
        """
        if df_1m is None or df_1m.empty or "open_time" not in df_1m.columns:
            asset_open = 0.0
            eval_close = 0.0
        else:
            window_df = df_1m[
                (df_1m["open_time"] >= window_open) & (df_1m["open_time"] < window_close)
            ]
            if window_df.empty or "open" not in window_df.columns:
                asset_open = 0.0
                eval_close = 0.0
            else:
                asset_open = float(window_df.iloc[0]["open"])
                if eval_minutes_left is not None:
                    eval_ts = pd.Timestamp(window_close) - pd.Timedelta(
                        minutes=float(eval_minutes_left)
                    )
                    if eval_ts.tzinfo is None:
                        eval_ts = eval_ts.tz_localize("UTC")
                    else:
                        eval_ts = eval_ts.tz_convert("UTC")
                    at_eval = window_df[window_df["open_time"] <= eval_ts]
                    if at_eval.empty:
                        eval_close = float(window_df.iloc[0]["close"])
                    else:
                        eval_close = float(at_eval.iloc[-1]["close"])
                else:
                    eval_close = float(window_df.iloc[0]["close"])

        t0 = pd.Timestamp(window_close)
        if eval_minutes_left is not None:
            t0 = t0 - pd.Timedelta(minutes=float(eval_minutes_left))
        else:
            t0 = pd.Timestamp(window_open)
        if t0.tzinfo is None:
            t0 = t0.tz_localize("UTC")
        else:
            t0 = t0.tz_convert("UTC")

        if pm_yes is not None and len(pm_yes) > 0:
            try:
                q = pm_yes.asof(t0)
            except (KeyError, ValueError, TypeError):
                q = float("nan")
            if q is not None and pd.notna(q):
                return float(max(0.01, min(0.99, float(q))))

        if asset_open > 0 and eval_close > 0:
            return self._proxy_yes_price_from_underlying(
                asset_open, eval_close, window_minutes
            )
        return 0.50

    def _yes_mid_at_window_open(
        self,
        *,
        window_open: pd.Timestamp,
        window_close: pd.Timestamp,
        window_minutes: int,
        df_1m: pd.DataFrame,
        pm_yes: Optional[pd.Series],
        eval_minutes_left: Optional[float] = None,
    ) -> float:
        return self._yes_mid_at_eval(
            window_open=window_open,
            window_close=window_close,
            window_minutes=window_minutes,
            df_1m=df_1m,
            pm_yes=pm_yes,
            eval_minutes_left=eval_minutes_left,
        )

    @staticmethod
    def _proxy_yes_price_from_underlying(
        asset_open: float,
        asset_current: float,
        window_minutes: int,
    ) -> float:
        """Approximate in-window YES price from underlying move vs window open.

        This is intentionally a proxy, not a claim of exact CLOB parity. It gives
        the crypto up/down backtest a live-like mark-to-market path so TP and
        near-expiry adverse-stop exits can be replayed instead of forcing every
        trade to binary settlement.
        """
        if asset_open <= 0:
            return 0.50
        move_pct = (asset_current - asset_open) / asset_open
        scale_pct = 0.0015 if window_minutes == 5 else 0.0025
        score = move_pct / max(scale_pct, 1e-6)
        yes_price = 0.50 + 0.45 * np.tanh(score)
        return float(np.clip(yes_price, 0.01, 0.99))

    def _settle_updown_with_live_exit_proxy(
        self,
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
        window_close: pd.Timestamp,
        action: str,
        entry_price: float,
        size: float,
        asset_open: float,
        fill_price: float,
        symbol: str,
        window_minutes: int,
        pm_yes: Optional[pd.Series] = None,
    ) -> Tuple[float, str, float, float, float, str]:
        """Approximate live crypto up/down exits from 1m replay bars.

        When ``pm_yes`` is set (PolymarketData 1m YES series), uses it for
        mark-to-market; otherwise ``_proxy_yes_price_from_underlying``.

        Returns:
            pnl, outcome, exit_price_token, asset_open_px, asset_close_px, exit_reason
        """
        w = df_1m[
            (df_1m["open_time"] >= window_open)
            & (df_1m["open_time"] < window_close)
        ].copy()
        if w.empty:
            return 0.0, "", 0.0, asset_open, asset_open, "unsettled"

        asset_close = float(w.iloc[-1]["close"])
        strategy_name = {
            "BTC": "bitcoin",
            "SOL": "sol_macro",
            "ETH": "eth_macro",
            "XRP": "xrp_macro",
            "HYPE": "hype_macro",
        }.get(str(symbol).upper(), "sol_macro")
        resolved = resolve_updown_exit_params_for_position(
            self._ude,
            strategy_name=strategy_name,
            window_size=f"{int(window_minutes)}m",
            entry_leg="NO" if action == "BUY_NO" else "YES",
            outcome="NO" if action == "BUY_NO" else "YES",
            opened_at=window_open.to_pydatetime() if hasattr(window_open, "to_pydatetime") else None,
            end_date=window_close.to_pydatetime() if hasattr(window_close, "to_pydatetime") else None,
        )

        up_stop_cents = cents_stop_for_entry_price(
            resolved.updown_stop_cents,
            fill_price,
            high_threshold=resolved.updown_high_entry_threshold,
            high_stop_cents=resolved.updown_stop_cents_high_entry,
        )

        mins_at_entry = float(window_minutes)
        entry_leg = "NO" if action == "BUY_NO" else "YES"
        peak_token = fill_price

        for _, row in w.iterrows():
            current_asset = float(row["close"])
            mins_remaining = (window_close - row["open_time"]).total_seconds() / 60.0
            current_yes = self._proxy_yes_price_from_underlying(
                asset_open=asset_open,
                asset_current=current_asset,
                window_minutes=window_minutes,
            )
            if pm_yes is not None and len(pm_yes) > 0:
                trow = pd.Timestamp(row["open_time"])
                if trow.tzinfo is None:
                    trow = trow.tz_localize("UTC")
                else:
                    trow = trow.tz_convert("UTC")
                try:
                    q = pm_yes.asof(trow)
                except (KeyError, ValueError, TypeError):
                    q = float("nan")
                if q is not None and pd.notna(q):
                    current_yes = float(max(0.01, min(0.99, float(q))))
            current_no = 1.0 - current_yes
            current_token = current_yes if action == "BUY_YES" else current_no
            peak_token = max(peak_token, current_token)
            pnl_pct = (current_token - fill_price) / fill_price if fill_price > 0 else 0.0
            peak_pnl_pct = (peak_token - fill_price) / fill_price if fill_price > 0 else 0.0

            effective_sl = effective_updown_stop_loss_pct(
                resolved.updown_stop_loss_pct,
                pnl_pct,
                peak_pnl_pct=peak_pnl_pct,
                in_profit_trigger_pct=resolved.updown_in_profit_stop_trigger_pct,
                tighten_to_pct=resolved.updown_in_profit_stop_tighten_to_pct,
            )

            if pnl_pct >= resolved.take_profit_pct:
                exit_fill, _ = self._simulate_fill(current_token, "SELL")
                exit_fill = max(0.01, min(0.99, exit_fill))
                pnl = (exit_fill - fill_price) * size
                return (
                    pnl,
                    "WIN" if pnl >= 0 else "LOSS",
                    exit_fill,
                    asset_open,
                    current_asset,
                    "take_profit",
                )

            if effective_sl > 0 and pnl_pct <= -effective_sl:
                exit_fill, _ = self._simulate_fill(current_token, "SELL")
                exit_fill = max(0.01, min(0.99, exit_fill))
                pnl = (exit_fill - fill_price) * size
                return (
                    pnl,
                    "WIN" if pnl >= 0 else "LOSS",
                    exit_fill,
                    asset_open,
                    current_asset,
                    "updown_stop_loss",
                )

            effective_window = scaled_exit_window_mins(
                resolved.updown_exit_window_mins,
                resolved.updown_exit_window_max_fraction,
                mins_at_entry,
            )
            if mins_remaining <= effective_window:
                adverse = adverse_for_updown_cents_time_stop(
                    entry_leg=entry_leg,
                    outcome="YES",
                    current_yes=current_yes,
                    current_no=current_no,
                    entry_price=fill_price,
                    up_stop_cents=up_stop_cents,
                )
                if adverse:
                    exit_fill, _ = self._simulate_fill(current_token, "SELL")
                    exit_fill = max(0.01, min(0.99, exit_fill))
                    pnl = (exit_fill - fill_price) * size
                    return (
                        pnl,
                        "WIN" if pnl >= 0 else "LOSS",
                        exit_fill,
                        asset_open,
                        current_asset,
                        "updown_time_stop",
                    )

        yes_won, asset_open_px, asset_close_px = self._settle(df_1m, window_open, window_close)
        if yes_won is None:
            return 0.0, "", 0.0, asset_open_px, asset_close_px, "unsettled"
        if action == "BUY_YES":
            if yes_won:
                return (
                    (1.0 - fill_price) * size,
                    "WIN",
                    1.0,
                    asset_open_px,
                    asset_close_px,
                    "settlement_yes",
                )
            return (
                -fill_price * size,
                "LOSS",
                0.0,
                asset_open_px,
                asset_close_px,
                "settlement_no",
            )
        if not yes_won:
            return (
                (1.0 - fill_price) * size,
                "WIN",
                1.0,
                asset_open_px,
                asset_close_px,
                "settlement_no",
            )
        return (
            -fill_price * size,
            "LOSS",
            0.0,
            asset_open_px,
            asset_close_px,
            "settlement_yes",
        )

    # -- slice helpers ---------------------------------------------------------

    @staticmethod
    def _before(df: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
        """All rows with open_time strictly BEFORE t -- no look-ahead."""
        if df is None or df.empty or "open_time" not in df.columns:
            return pd.DataFrame()
        target = pd.Timestamp(t)
        if target.tzinfo is None:
            target = target.tz_localize("UTC")
        else:
            target = target.tz_convert("UTC")
        if "_open_time_ns" in df.columns:
            times = df["_open_time_ns"].to_numpy()
        else:
            times = pd.to_datetime(df["open_time"], utc=True).astype("int64").to_numpy()
        idx = int(np.searchsorted(times, target.value, side="left"))
        return df.iloc[:idx]

    @staticmethod
    def _prepare_replay_data_frames(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """Normalize replay frames once so per-window slices can use searchsorted."""
        out: Dict[str, pd.DataFrame] = {}
        for iv, df in (data or {}).items():
            if df is None or df.empty or "open_time" not in df.columns:
                out[iv] = df
                continue
            prepared = df.copy()
            prepared["open_time"] = pd.to_datetime(prepared["open_time"], utc=True)
            prepared = prepared.sort_values("open_time").reset_index(drop=True)
            prepared["_open_time_ns"] = prepared["open_time"].astype("int64")
            out[iv] = prepared
        return out

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
            elif move_pct > 0.01:
                result.m5_direction = "LEAN_UP"
            elif move_pct < -0.01:
                result.m5_direction = "LEAN_DOWN"

            m5_age_minutes = (
                (m5_early.iloc[-1]["open_time"] - window_open).total_seconds() / 60.0
            )
            result.m5_in_prediction_window = 3.0 <= m5_age_minutes <= 4.0

        return result

    # -- indicator reconstruction ----------------------------------------------

    def _build_ta(
        self, t: pd.Timestamp, data: Dict[str, pd.DataFrame],
        htf_key: str = "4h",
        *,
        include_trend_sabre: bool = True,
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
        # Trend Sabre is expensive and only used by BTC paths.  Alt-family
        # ETH/SOL/XRP/HYPE paths use EMA/MACD/RSI, so skip it there.
        sabre = self._svc.calc_trend_sabre(df_htf) if include_trend_sabre else TrendSabreResult()
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

        # -- 30m MACD (Binance/native or resampled from 15m) -------------------
        df_30m = self._before(data.get("30m", pd.DataFrame()), t)
        if (df_30m is None or df_30m.empty) and len(df_15m) >= 60:
            df_30m = (
                df_15m.set_index("open_time")
                .resample("30min")
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
        macd_30m = self._svc.calc_macd(df_30m) if len(df_30m) >= 30 else MACDResult()

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
        sma_1h_20 = (
            float(BTCPriceService._calc_sma(df_1h["close"], 20).iloc[-1])
            if len(df_1h) >= 20
            else 0.0
        )
        btc_1h_close = float(df_1h["close"].iloc[-1]) if len(df_1h) else 0.0

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
            sma_1h_20=sma_1h_20,
            btc_1h_close=btc_1h_close,
            ema_9=ema_9, ema_21=ema_21, ema_50=ema_50, ema_200=ema_200,
            rsi_14=rsi_14,
            macd_4h=macd_4h,
            macd_1h=macd_1h,
            macd_15m=macd_15m,
            macd_30m=macd_30m,
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
        self,
        ta: TechnicalAnalysis,
        df_15m: pd.DataFrame,
        df_1h: Optional[pd.DataFrame] = None,
    ) -> str:
        """SOL-family 3-vote system matching sol_macro._get_macro_trend().

        Vote 1: 1H trend from replayed 1H candles when available
        Vote 2: 15m EMA alignment (ema_9 > ema_21 > ema_50)
        Vote 3: 15m RSI zone (>55 bull, <45 bear)
        """
        bull = bear = 0

        h1_trend = self._alt_1h_trend_from_df(df_1h)
        if h1_trend == "BULLISH":
            bull += 1
        elif h1_trend == "BEARISH":
            bear += 1
        elif ta.ema_9 > ta.ema_21:
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

    @staticmethod
    def _alt_1h_trend_from_df(df_1h: Optional[pd.DataFrame]) -> str:
        """Approximate live alt 1H trend from replay candles."""
        if df_1h is None or len(df_1h) < 21:
            return "NEUTRAL"
        close_1h = df_1h["close"]
        price = float(close_1h.iloc[-1])
        ema_9 = float(BTCPriceService._calc_ema(close_1h, 9).iloc[-1])
        ema_21 = float(BTCPriceService._calc_ema(close_1h, 21).iloc[-1])
        ema_50 = (
            float(BTCPriceService._calc_ema(close_1h, 50).iloc[-1])
            if len(df_1h) >= 50
            else 0.0
        )
        rsi = float(BTCPriceService._calc_rsi(close_1h, 14).iloc[-1])

        bullish_score = 0
        if price > ema_9:
            bullish_score += 1
        if ema_9 > ema_21:
            bullish_score += 1
        if ema_50 > 0 and ema_21 > ema_50:
            bullish_score += 1
        if rsi > 55:
            bullish_score += 1
        elif rsi < 45:
            bullish_score -= 1

        if bullish_score >= 3:
            return "BULLISH"
        if bullish_score <= -1 or (bullish_score == 0 and rsi < 45):
            bearish_count = 0
            if price < ema_9:
                bearish_count += 1
            if ema_9 < ema_21:
                bearish_count += 1
            if ema_50 > 0 and ema_21 < ema_50:
                bearish_count += 1
            if rsi < 45:
                bearish_count += 1
            if bearish_count >= 2:
                return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def _macd_bearish_momentum_ok(m: Optional[MACDResult]) -> bool:
        if m is None:
            return False
        if m.crossover == "BEARISH_CROSS":
            return True
        if (not m.histogram_rising) and float(m.histogram) < 0:
            return True
        return float(m.macd_line) < float(m.signal_line) and float(m.histogram) <= 0

    @staticmethod
    def _macd_bullish_momentum_ok(m: Optional[MACDResult]) -> bool:
        """Mirror of bearish — used for symmetric bullish-rally LTF replay gating."""
        if m is None:
            return False
        if m.crossover == "BULLISH_CROSS":
            return True
        if m.histogram_rising and float(m.histogram) > 0:
            return True
        return float(m.macd_line) > float(m.signal_line) and float(m.histogram) >= 0

    @staticmethod
    def _side_from_bias(bias: Optional[str]) -> Optional[str]:
        if bias == "BULLISH":
            return "LONG"
        if bias == "BEARISH":
            return "SHORT"
        return None

    @staticmethod
    def _recent_move_pct(
        df_1m: Optional[pd.DataFrame],
        window_open: pd.Timestamp,
        minutes: int,
    ) -> float:
        if df_1m is None or df_1m.empty or minutes <= 0:
            return 0.0
        start = window_open - pd.Timedelta(minutes=minutes)
        bars = df_1m[(df_1m["open_time"] >= start) & (df_1m["open_time"] < window_open)]
        if bars.empty:
            return 0.0
        open_px = float(bars.iloc[0]["open"])
        close_px = float(bars.iloc[-1]["close"])
        if open_px <= 0:
            return 0.0
        return ((close_px - open_px) / open_px) * 100.0

    @staticmethod
    def _eth_signal_15m_proxy(df_15m: pd.DataFrame) -> float:
        if df_15m is None or df_15m.empty:
            return 0.50
        bar = df_15m.iloc[-1]
        return UpdownBacktestEngine._proxy_yes_price_from_underlying(
            float(bar["open"]),
            float(bar["close"]),
            15,
        )

    @staticmethod
    def _resolve_eth_market_side(
        base_side: str,
        btc_htf_bias: Optional[str],
        market_yes_price: float,
        strategy_cfg: Dict[str, Any],
    ) -> tuple[str, str]:
        direction_source = str(strategy_cfg.get("direction_source", "hybrid")).strip().lower()
        if direction_source not in {"btc", "hybrid", "signal_first"}:
            direction_source = "btc"
        if direction_source == "btc":
            return base_side, "alt_1h_legacy_btc_mode"

        signal_15m_long_threshold = float(strategy_cfg.get("signal_15m_long_threshold", 0.55))
        signal_15m_short_threshold = float(strategy_cfg.get("signal_15m_short_threshold", 0.45))

        if direction_source == "signal_first":
            if market_yes_price >= signal_15m_long_threshold and btc_htf_bias != "BEARISH":
                return "LONG", "signal_first_long"
            if market_yes_price <= signal_15m_short_threshold and btc_htf_bias != "BULLISH":
                return "SHORT", "signal_first_short"
            return base_side, "signal_first_fallback"

        if base_side == "LONG" and market_yes_price >= signal_15m_long_threshold:
            return base_side, "hybrid_alt_long_confirmed"
        if base_side == "SHORT" and market_yes_price <= signal_15m_short_threshold:
            return base_side, "hybrid_alt_short_confirmed"
        return base_side, "hybrid_alt_first"

    def _btc_htf_bias_or_neutral(
        self,
        btc_ta: Optional[TechnicalAnalysis],
        *,
        min_hist: float,
    ) -> Optional[str]:
        """Unit tests may stub partial BTC TA objects; replay should degrade, not crash."""
        if btc_ta is None:
            return None
        required = ("trend_sabre", "macd_4h", "current_price")
        if any(not hasattr(btc_ta, attr) for attr in required):
            return "NEUTRAL"
        return self._get_htf_bias(btc_ta, min_hist=min_hist)

    @staticmethod
    def _btc_1h_regime_min_edge_mult(
        strategy_cfg: Dict[str, Any],
        btc_ta: Optional[TechnicalAnalysis],
    ) -> float:
        if btc_ta is None:
            return 1.0
        gates = dict(strategy_cfg.get("btc_1h_regime_gates") or {})
        if not gates.get("enabled", False):
            return 1.0
        sma = float(getattr(btc_ta, "sma_1h_20", 0.0) or 0.0)
        price = float(getattr(btc_ta, "btc_1h_close", 0.0) or getattr(btc_ta, "current_price", 0.0) or 0.0)
        if price <= 0 or sma <= 0:
            regime = "RANGE"
        else:
            band = float(gates.get("range_band_pct", 0.0012))
            dist_pct = (price - sma) / sma
            if abs(dist_pct) <= band:
                regime = "RANGE"
            else:
                regime = "BULL" if dist_pct > band else "BEAR"
        mults = dict(gates.get("min_edge_mult") or {})
        defaults = {"BULL": 1.0, "RANGE": 1.25, "BEAR": 1.40}
        return float(mults.get(regime, defaults.get(regime, 1.0)))

    def _alt_macd_4h_from_1h(self, df_1h: Optional[pd.DataFrame]) -> Optional[MACDResult]:
        """Resample alt 1H bars to 4H and compute MACD. Returns None if insufficient data.

        Used by replay-path 4H-hist override gate; live path consumes
        SOLAnalysis.macd_4h directly via a native 4H fetch.
        """
        if df_1h is None or df_1h.empty or "open_time" not in df_1h.columns or len(df_1h) < 120:
            return None
        df_4h = (
            df_1h.set_index("open_time")
            .resample("4h")
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
        if len(df_4h) < 30:
            return None
        return self._svc.calc_macd(df_4h)

    def _buy_no_ltf_override_replay(
        self,
        ta: TechnicalAnalysis,
        df_5m: pd.DataFrame,
        btc_1m: Optional[pd.DataFrame],
        window_open: pd.Timestamp,
        strategy_cfg: Dict[str, Any],
        df_1h: Optional[pd.DataFrame] = None,
    ) -> tuple[bool, str]:
        ltf_on = bool(strategy_cfg.get("buy_no_ltf_override_enabled", False))
        hist_on = bool(strategy_cfg.get("buy_no_4h_hist_override_enabled", False))
        if not ltf_on and not hist_on:
            return False, "disabled"
        if ltf_on:
            if len(df_5m) < _MIN_5M_BARS:
                return False, "5m_insufficient"
            bearish_15m = self._macd_bearish_momentum_ok(ta.macd_15m)
            bearish_5m = self._macd_bearish_momentum_ok(self._svc.calc_macd(df_5m))
            rsi_max = float(strategy_cfg.get("buy_no_ltf_override_rsi_max", 45.0))
            rsi_ok = float(ta.rsi_14 or 50.0) <= rsi_max
            btc_cap = float(strategy_cfg.get("buy_no_ltf_override_max_btc_5m_pct", 0.0))
            btc_move_5m_pct = self._recent_move_pct(btc_1m, window_open, 5)
            btc_ok = btc_move_5m_pct <= btc_cap
            if bearish_15m and bearish_5m and rsi_ok and btc_ok:
                return True, "bearish_ltf_override"
        if hist_on:
            macd_4h = self._alt_macd_4h_from_1h(df_1h)
            if macd_4h is not None and not macd_4h.histogram_rising:
                return True, "4h_hist_override"
        return False, "no_override"

    def _bullish_rally_ltf_override_replay(
        self,
        ta: TechnicalAnalysis,
        df_5m: pd.DataFrame,
        btc_1m: Optional[pd.DataFrame],
        window_open: pd.Timestamp,
        strategy_cfg: Dict[str, Any],
        df_1h: Optional[pd.DataFrame] = None,
    ) -> tuple[bool, str]:
        """Mirror of _buy_no_ltf_override_replay for the LONG side."""
        ltf_on = bool(strategy_cfg.get("buy_yes_ltf_override_enabled", False))
        hist_on = bool(strategy_cfg.get("buy_yes_4h_hist_override_enabled", False))
        if not ltf_on and not hist_on:
            return False, "disabled"
        if ltf_on:
            if len(df_5m) < _MIN_5M_BARS:
                return False, "5m_insufficient"
            bullish_15m = self._macd_bullish_momentum_ok(ta.macd_15m)
            bullish_5m = self._macd_bullish_momentum_ok(self._svc.calc_macd(df_5m))
            rsi_min = float(strategy_cfg.get("buy_yes_ltf_override_rsi_min", 55.0))
            rsi_ok = float(ta.rsi_14 or 50.0) >= rsi_min
            btc_floor = float(strategy_cfg.get("buy_yes_ltf_override_min_btc_5m_pct", 0.0))
            btc_move_5m_pct = self._recent_move_pct(btc_1m, window_open, 5)
            btc_ok = btc_move_5m_pct >= btc_floor
            if bullish_15m and bullish_5m and rsi_ok and btc_ok:
                return True, "bullish_ltf_override"
        if hist_on:
            macd_4h = self._alt_macd_4h_from_1h(df_1h)
            if macd_4h is not None and macd_4h.histogram_rising:
                return True, "4h_hist_override"
        return False, "no_override"

    def _resolve_side_with_ltf_overrides_replay(
        self,
        *,
        primary_htf_bias: str,
        ta: TechnicalAnalysis,
        df_5m: pd.DataFrame,
        btc_1m: Optional[pd.DataFrame],
        window_open: pd.Timestamp,
        strategy_cfg: Dict[str, Any],
        df_1h: Optional[pd.DataFrame] = None,
    ) -> tuple[Optional[str], str, str]:
        """Replay-side mirror of sol_macro._resolve_allowed_side_with_ltf_overrides.

        Additive-only: defaults always fire, exceptions flip side when their LTF
        confirms and the opposite-direction LTF does not. Never returns None for
        BULLISH/BEARISH inputs.
        """
        bullish_ok, _ = self._bullish_rally_ltf_override_replay(
            ta, df_5m, btc_1m, window_open, strategy_cfg, df_1h=df_1h
        )
        bearish_ok, _ = self._buy_no_ltf_override_replay(
            ta, df_5m, btc_1m, window_open, strategy_cfg, df_1h=df_1h
        )
        if primary_htf_bias == "BULLISH":
            if bearish_ok and not bullish_ok:
                return "SHORT", "bearish_dip_exception", "bearish_ltf_override"
            return "LONG", "bullish_rally_default", "default_long"
        if primary_htf_bias == "BEARISH":
            if bullish_ok and not bearish_ok:
                return "LONG", "bullish_rally_exception", "bullish_ltf_override"
            return "SHORT", "bearish_dip_default", "default_short"
        return None, "skip", "neutral_htf_no_resolver"

    def _resolve_alt_replay_direction(
        self,
        *,
        symbol: str,
        ta: TechnicalAnalysis,
        df_1h: pd.DataFrame,
        df_15m: pd.DataFrame,
        df_5m: pd.DataFrame,
        alt_1m: pd.DataFrame,
        btc_ta: Optional[TechnicalAnalysis],
        btc_data: Optional[Dict[str, pd.DataFrame]],
        window_open: pd.Timestamp,
        strategy_cfg: Dict[str, Any],
    ) -> ReplayDirectionDecision:
        decision = ReplayDirectionDecision()
        btc_htf_bias = self._btc_htf_bias_or_neutral(
            btc_ta,
            min_hist=float(
                self.config.get("strategies", {}).get("bitcoin", {}).get("min_4h_hist_magnitude", 20.0)
            ),
        )

        btc_1m = None if not btc_data else btc_data.get("1m")
        alt_1h_trend = self._alt_1h_trend_from_df(df_1h)
        macro_trend = self._get_sol_htf_bias(ta, df_15m, df_1h)
        decision.alt_htf_bias = alt_1h_trend if symbol == "ETH" else macro_trend

        btc_move_5m = self._recent_move_pct(btc_1m, window_open, 5)
        btc_move_15m = self._recent_move_pct(btc_1m, window_open, 15)
        alt_move_5m = self._recent_move_pct(alt_1m, window_open, 5)
        alt_move_15m = self._recent_move_pct(alt_1m, window_open, 15)
        spike_5m_floor = float(strategy_cfg.get("btc_spike_floor_pct_5m", 0.3))
        spike_15m_floor = float(strategy_cfg.get("btc_spike_floor_pct_15m", 0.8))
        lag_min = float(strategy_cfg.get("lag_signal_min_pct", 0.2))
        neutral_requires_catalyst = bool(strategy_cfg.get("neutral_macro_require_spike_or_lag", False))

        btc_spike_detected = abs(btc_move_5m) >= spike_5m_floor or abs(btc_move_15m) >= spike_15m_floor
        btc_move_for_lag = btc_move_15m if abs(btc_move_15m) >= abs(btc_move_5m) else btc_move_5m
        alt_move_for_lag = alt_move_15m if abs(btc_move_15m) >= abs(btc_move_5m) else alt_move_5m
        lag_raw = abs(btc_move_for_lag) - abs(alt_move_for_lag)
        lag_direction = "LONG" if btc_move_for_lag > 0 else "SHORT" if btc_move_for_lag < 0 else None
        lag_opportunity = lag_direction is not None and lag_raw >= lag_min
        decision.lag_magnitude = btc_move_for_lag - alt_move_for_lag

        if symbol == "ETH":
            if alt_1h_trend in {"BULLISH", "BEARISH"}:
                decision.allowed_side = self._side_from_bias(alt_1h_trend)
                decision.primary_htf_bias = alt_1h_trend
                decision.side_source = "alt_1h_primary"
                decision.skip_btc_follow_1h = True
                resolver_active = (
                    bool(strategy_cfg.get("buy_yes_ltf_override_enabled", False))
                    or bool(strategy_cfg.get("buy_no_ltf_override_enabled", False))
                    or bool(strategy_cfg.get("buy_yes_4h_hist_override_enabled", False))
                    or bool(strategy_cfg.get("buy_no_4h_hist_override_enabled", False))
                )
                if resolver_active:
                    r_side, r_source, r_detail = self._resolve_side_with_ltf_overrides_replay(
                        primary_htf_bias=alt_1h_trend,
                        ta=ta,
                        df_5m=df_5m,
                        btc_1m=btc_1m,
                        window_open=window_open,
                        strategy_cfg=strategy_cfg,
                        df_1h=df_1h,
                    )
                    # Additive-only resolver: r_side never None for BULL/BEAR.
                    if r_side is not None:
                        decision.allowed_side = r_side
                        decision.side_source = r_source
                elif alt_1h_trend == "BULLISH":
                    override, _ = self._buy_no_ltf_override_replay(
                        ta, df_5m, btc_1m, window_open, strategy_cfg, df_1h=df_1h
                    )
                    if override:
                        decision.allowed_side = "SHORT"
                        decision.side_source = "bearish_ltf_override"
            else:
                if macro_trend in {"BULLISH", "BEARISH"}:
                    decision.allowed_side = self._side_from_bias(macro_trend)
                    decision.primary_htf_bias = macro_trend
                    decision.side_source = "eth_macro_trend_fallback"
                    decision.skip_btc_follow_1h = True
                elif btc_spike_detected:
                    decision.allowed_side = "LONG" if btc_move_5m > 0 else "SHORT"
                    decision.side_source = "btc_spike"
                    decision.skip_btc_follow_1h = True
                elif lag_opportunity and lag_direction is not None:
                    decision.allowed_side = lag_direction
                    decision.side_source = "lag_fallback"
                    decision.skip_btc_follow_1h = True
                elif btc_htf_bias in {"BULLISH", "BEARISH"} and not neutral_requires_catalyst:
                    decision.allowed_side = self._side_from_bias(btc_htf_bias)
                    decision.side_source = "btc_htf_fallback"
                else:
                    decision.skip_reason = (
                        "neutral_macro_no_catalyst" if neutral_requires_catalyst else "htf_neutral"
                    )
                    return decision
                if not decision.primary_htf_bias:
                    decision.primary_htf_bias = "NEUTRAL"

            market_side, market_source = self._resolve_eth_market_side(
                decision.allowed_side,
                btc_htf_bias,
                self._eth_signal_15m_proxy(df_15m),
                strategy_cfg,
            )
            decision.allowed_side = market_side
            decision.side_source = market_source
            return decision

        primary_htf_bias = macro_trend if macro_trend != "NEUTRAL" else (btc_htf_bias or macro_trend)
        decision.primary_htf_bias = primary_htf_bias
        if primary_htf_bias in {"BULLISH", "BEARISH"}:
            decision.allowed_side = self._side_from_bias(primary_htf_bias)
            decision.side_source = "primary_htf"
            resolver_active = (
                bool(strategy_cfg.get("buy_yes_ltf_override_enabled", False))
                or bool(strategy_cfg.get("buy_no_ltf_override_enabled", False))
                or bool(strategy_cfg.get("buy_yes_4h_hist_override_enabled", False))
                or bool(strategy_cfg.get("buy_no_4h_hist_override_enabled", False))
            )
            if resolver_active:
                r_side, r_source, r_detail = self._resolve_side_with_ltf_overrides_replay(
                    primary_htf_bias=primary_htf_bias,
                    ta=ta,
                    df_5m=df_5m,
                    btc_1m=btc_1m,
                    window_open=window_open,
                    strategy_cfg=strategy_cfg,
                    df_1h=df_1h,
                )
                if r_side is None:
                    decision.allowed_side = None
                    decision.side_source = r_source
                    decision.skip_reason = f"ltf_resolver_skip:{r_detail}"
                    return decision
                decision.allowed_side = r_side
                decision.side_source = r_source
            elif primary_htf_bias == "BULLISH":
                override, _ = self._buy_no_ltf_override_replay(
                    ta, df_5m, btc_1m, window_open, strategy_cfg, df_1h=df_1h
                )
                if override:
                    decision.allowed_side = "SHORT"
                    decision.side_source = "bearish_ltf_override"
            return decision

        if btc_spike_detected:
            decision.allowed_side = "LONG" if btc_move_5m > 0 else "SHORT"
            decision.side_source = "btc_spike"
            return decision
        if lag_opportunity and lag_direction is not None:
            decision.allowed_side = lag_direction
            decision.side_source = "lag_fallback"
            return decision
        if neutral_requires_catalyst:
            decision.skip_reason = "neutral_macro_no_catalyst"
            return decision
        decision.allowed_side = self._side_from_bias(alt_1h_trend)
        if decision.allowed_side is None:
            decision.skip_reason = "neutral_no_alt_bias"
            return decision
        decision.side_source = "alt_1h_fallback"
        return decision

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
    def _sol_ltf_strength_m(m: MACDResult, allowed_side: str) -> Tuple[bool, float]:
        """SOL-family 15m MACD strength on a single MACD bundle (live parity)."""
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

    @staticmethod
    def _sol_ltf_strength(ta: TechnicalAnalysis, allowed_side: str) -> Tuple[bool, float]:
        """15m MACD confirmation -- SOL-family live weights, threshold 0.50.

        Differences from BTC:
          - hist rising (not flip): +0.15 (BTC uses +0.20)
          - MACD > signal:          +0.10 (BTC uses +0.15)
          - confirmed threshold:     0.50 (live anti-LTF gate)
        """
        return UpdownBacktestEngine._sol_ltf_strength_m(ta.macd_15m, allowed_side)

    @staticmethod
    def _passes_15m_iql_macd(m: MACDResult, allowed_side: str, hist_floor: float) -> bool:
        """15m Indicator Quality Layer — matches live sol_macro._passes_15m_iql."""
        confirmed, _ = UpdownBacktestEngine._sol_ltf_strength_m(m, allowed_side)
        if confirmed:
            return True
        hist = float(m.histogram)
        if allowed_side == "LONG":
            return m.crossover == "BULLISH_CROSS" or (
                hist >= hist_floor and m.histogram_rising
            )
        return m.crossover == "BEARISH_CROSS" or (
            hist <= -hist_floor and not m.histogram_rising
        )

    @staticmethod
    def _btc_alt_corr_1h_approx(
        window_open: pd.Timestamp,
        btc_1m: pd.DataFrame,
        alt_1m: pd.DataFrame,
    ) -> Optional[float]:
        """Pearson corr of 1m returns over last ≤60 overlapping bars (< window_open).

        Mirrors live SOLBTCService.calc_correlation correlation_1h windowing.
        """
        if btc_1m.empty or alt_1m.empty:
            return None
        btc_sub = btc_1m.loc[btc_1m["open_time"] < window_open, ["open_time", "close"]].rename(
            columns={"close": "btc_c"}
        )
        alt_sub = alt_1m.loc[alt_1m["open_time"] < window_open, ["open_time", "close"]].rename(
            columns={"close": "alt_c"}
        )
        if btc_sub.empty or alt_sub.empty:
            return None
        merged = btc_sub.merge(alt_sub, on="open_time", how="inner").tail(60)
        ret = merged[["btc_c", "alt_c"]].pct_change().dropna()
        if len(ret) < 10:
            return None
        btc_r = ret["btc_c"].to_numpy()
        alt_r = ret["alt_c"].to_numpy()
        if float(np.std(btc_r)) <= 1e-12 or float(np.std(alt_r)) <= 1e-12:
            return 0.0
        return float(np.corrcoef(btc_r, alt_r)[0, 1])

    # ==========================================================================
    # BTC 15m edge (matches bitcoin.py 15m updown path exactly)
    # ==========================================================================

    def _edge_15m(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        ltf_strength: float,
        htf_bias: str = "NEUTRAL",
        yes_price: float = 0.50,
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
        macd_1h = ta.macd_1h
        mom = ta.candle_momentum

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

        # Histogram gate parity with live bitcoin.py:
        # allow a 1H local recovery pass when 4H is decelerating against the side.
        if allowed_side == "LONG" and not macd_4h.histogram_rising:
            if not macd_1h.histogram_rising:
                return 0.0, 0.0
        if allowed_side == "SHORT" and macd_4h.histogram_rising:
            if macd_1h.histogram_rising:
                return 0.0, 0.0

        # LTF adj (anti-LTF gate already applied in run())
        ltf_adj = ltf_strength * 0.20
        est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj

        # Timing bonus parity with live bitcoin.py.
        timing_bonus = 0.0
        if allowed_side == "LONG":
            if mom.m15_direction in ("SPIKE_UP", "DRIFT_UP"):
                timing_bonus += 0.08 if "SPIKE" in mom.m15_direction else 0.04
            elif mom.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                timing_bonus -= 0.05
            if mom.m5_direction in ("SPIKE_UP", "DRIFT_UP"):
                timing_bonus += 0.04 if "SPIKE" in mom.m5_direction else 0.02
        else:
            if mom.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                timing_bonus += 0.08 if "SPIKE" in mom.m15_direction else 0.04
            elif mom.m15_direction in ("SPIKE_UP", "DRIFT_UP"):
                timing_bonus -= 0.05
            if mom.m5_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                timing_bonus += 0.04 if "SPIKE" in mom.m5_direction else 0.02
        if mom.m15_in_prediction_window:
            timing_bonus += 0.03
        if mom.m5_in_prediction_window:
            timing_bonus += 0.02
        est_prob_up += timing_bonus if allowed_side == "LONG" else -timing_bonus

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
        confidence = min(0.85, 0.50 + ltf_strength * 0.20 + abs(timing_bonus))
        return est_prob_up, confidence

    # ==========================================================================
    # SOL 15m edge (matches sol_macro.py 15m updown path)
    # ==========================================================================

    def _edge_15m_sol(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        ltf_strength: float,
        *,
        iql_enabled: bool = False,
        iql_hist_floor: float = 0.03,
        yes_price: float = 0.50,
    ) -> Tuple[float, float]:
        """SOL 15m edge -- macro boost +/-0.07, LTF*0.22, 1H histogram gate.

        Omits: lag/spike (requires live BTC feed), correlation dampen.
        """
        if iql_enabled and not UpdownBacktestEngine._passes_15m_iql_macd(
            ta.macd_15m, allowed_side, iql_hist_floor
        ):
            return 0.0, 0.0

        macd_1h = ta.macd_4h   # For SOL, macd_4h is computed from 1H data

        est_prob_up = 0.50

        # Macro trend boost (matches live sol_macro 15m: +/-0.07)
        # htf_bias is already known to be BULLISH or BEARISH at this point
        if allowed_side == "LONG":
            est_prob_up += 0.07
        else:
            est_prob_up -= 0.07

        # 1H histogram gate (matches current live sol_macro behavior).
        # Keep participation unchanged relative to live:
        # LONG passes on rising histogram or positive histogram;
        # SHORT passes on flat/down histogram or negative histogram.
        h1_bull_ok = macd_1h.histogram_rising or macd_1h.histogram > 0
        h1_bear_ok = (not macd_1h.histogram_rising) or macd_1h.histogram < 0
        if allowed_side == "LONG" and not h1_bull_ok:
            return 0.0, 0.0
        if allowed_side == "SHORT" and not h1_bear_ok:
            return 0.0, 0.0

        # LTF adj (anti-LTF gate already applied in run())
        ltf_adj = ltf_strength * 0.22
        est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj

        # RSI extremes (matches live sol_macro 15m: >75/-0.03, <25/+0.03)
        if   ta.rsi_14 > 75: est_prob_up -= 0.03
        elif ta.rsi_14 < 25: est_prob_up += 0.03

        est_prob_up = max(0.10, min(0.90, est_prob_up))
        if allowed_side == "LONG":
            edge = est_prob_up - yes_price
        else:
            edge = yes_price - est_prob_up
        # Confidence: matches live = min(0.85, 0.50 + ltf_strength * 0.22 + lag_conf + timing*0.5)
        # lag_conf and timing = 0 in backtest
        confidence = min(0.85, 0.50 + ltf_strength * 0.22)
        return edge, confidence

    # ==========================================================================
    # BTC 5m candle momentum (live calc_candle_momentum early-candle read)
    # ==========================================================================

    @staticmethod
    def _calc_m5_momentum(
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
        allowed_side: str,
    ) -> Tuple[str, float]:
        """Derive m5_direction and m5_adj from early-candle 1m bars.

        Mirrors live ``calc_candle_momentum()``: first 90s of the current 5m
        candle (same early window live reads at scan time, regardless of
        ``eval_minutes_left`` within the entry band).

        Thresholds (btc_price_service.calc_candle_momentum / former _core):
            SPIKE : abs(move_pct) > 0.08 %
            DRIFT : abs(move_pct) > 0.03 %
            LEAN  : abs(move_pct) > 0.01 %
        """
        if df_1m.empty:
            return "NONE", 0.0

        cutoff = window_open + pd.Timedelta(seconds=90)
        early = df_1m[(df_1m["open_time"] >= window_open) & (df_1m["open_time"] < cutoff)]
        if early.empty:
            return "NONE", 0.0

        candle_open  = float(early.iloc[0]["open"])
        early_close  = float(early.iloc[-1]["close"])
        if candle_open <= 0:
            return "NONE", 0.0

        move_pct = (early_close - candle_open) / candle_open * 100

        if   move_pct >  0.08: direction = "SPIKE_UP"
        elif move_pct < -0.08: direction = "SPIKE_DOWN"
        elif move_pct >  0.03: direction = "DRIFT_UP"
        elif move_pct < -0.03: direction = "DRIFT_DOWN"
        elif move_pct >  0.01: direction = "LEAN_UP"
        elif move_pct < -0.01: direction = "LEAN_DOWN"
        else:                  direction = "NONE"

        return direction, score_m5_direction(direction, allowed_side)

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
        corr_1h: Optional[float] = None,
        yes_price: float = 0.50,
        eval_minutes_left: Optional[float] = None,
        window_minutes: int = 5,
        htf_bias: str = "NEUTRAL",
    ) -> Tuple[float, float]:
        """Estimate edge for a 5m updown window.

        BTC: HTF boost + 4H hist gate + candle momentum (matches bitcoin.py 5m path).
        SOL: macro boost + 1H hist gate + 5m MACD (matches sol_macro.py 5m path).
        """
        macd_htf = ta.macd_4h   # 4H for BTC, 1H for SOL (built from htf_key data)

        if symbol == "BTC":
            edge, confidence, _raw, _lane = self._edge_5m_btc(
                ta,
                allowed_side,
                df_1m,
                window_open,
                macd_htf,
                yes_price=yes_price,
                eval_minutes_left=eval_minutes_left,
                window_minutes=window_minutes,
                htf_bias=htf_bias,
            )
            return edge, confidence
        elif symbol == "ETH":
            min_buy = self.min_positive_m5_adj_eth_5m
            min_sell = self.min_positive_m5_adj_eth_5m_sell
            corr_min = self.sell_5m_min_corr_eth
            return self._edge_5m_sol(
                ta,
                allowed_side,
                df_5m,
                macd_htf,
                min_buy=min_buy,
                min_sell=min_sell,
                sell_5m_min_corr=corr_min,
                corr_1h=corr_1h,
                yes_price=yes_price,
            )
        elif symbol == "XRP":
            min_buy = self.min_positive_m5_adj_xrp_5m
            min_sell = self.min_positive_m5_adj_xrp_5m_sell
            corr_min = self.sell_5m_min_corr_xrp
            return self._edge_5m_sol(
                ta,
                allowed_side,
                df_5m,
                macd_htf,
                min_buy=min_buy,
                min_sell=min_sell,
                sell_5m_min_corr=corr_min,
                corr_1h=corr_1h,
                yes_price=yes_price,
            )
        elif symbol == "HYPE":
            min_buy = self.min_positive_m5_adj_hype_5m
            min_sell = self.min_positive_m5_adj_hype_5m_sell
            corr_min = self.sell_5m_min_corr_hype
            return self._edge_5m_sol(
                ta,
                allowed_side,
                df_5m,
                macd_htf,
                min_buy=min_buy,
                min_sell=min_sell,
                sell_5m_min_corr=corr_min,
                corr_1h=corr_1h,
                yes_price=yes_price,
            )
        else:
            min_buy = self.min_positive_m5_adj_sol_5m
            min_sell = self.min_positive_m5_adj_sol_5m_sell
            corr_min = self.sell_5m_min_corr_sol
            return self._edge_5m_sol(
                ta,
                allowed_side,
                df_5m,
                macd_htf,
                min_buy=min_buy,
                min_sell=min_sell,
                sell_5m_min_corr=corr_min,
                corr_1h=corr_1h,
                yes_price=yes_price,
            )

    def _edge_5m_btc(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        df_1m: pd.DataFrame,
        window_open: pd.Timestamp,
        macd_4h: MACDResult,
        *,
        yes_price: float = 0.50,
        eval_minutes_left: Optional[float] = None,
        window_minutes: int = 5,
        htf_bias: str = "NEUTRAL",
    ) -> Tuple[float, float, float, str]:
        """BTC 5m raw quant + live-style lane calibration -> edge."""
        m5_dir, _ = self._calc_m5_momentum(
            df_1m if df_1m is not None else pd.DataFrame(),
            window_open,
            allowed_side,
        )
        if eval_minutes_left is not None:
            m5_age = m5_candle_age_minutes(window_minutes, eval_minutes_left)
            in_pred = m5_in_prediction_window_at_age(m5_age)
        else:
            in_pred = bool(ta.candle_momentum.m5_in_prediction_window)

        q = compute_btc_5m_quant(
            sabre=ta.trend_sabre,
            macd_4h=macd_4h,
            macd_1h=ta.macd_1h,
            rsi_14=ta.rsi_14,
            allowed_side=allowed_side,
            yes_price=yes_price,
            m5_direction=m5_dir,
            m5_in_prediction_window=in_pred,
        )
        if not q.hist_gate_allowed or q.rsi_blocked:
            return 0.0, q.confidence, q.est_prob_up, ""

        edge, lane_id, _cal_p = edge_from_raw_est_prob(
            self._lane_calibrator,
            q.est_prob_up,
            yes_price,
            allowed_side,
            strategy="bitcoin",
            window_minutes=window_minutes,
            htf_bias=htf_bias,
            signal_reason="UPDOWN_5m",
        )
        return edge, q.confidence, q.est_prob_up, lane_id

    def _edge_5m_sol(
        self,
        ta: TechnicalAnalysis,
        allowed_side: str,
        df_5m: pd.DataFrame,
        macd_1h: MACDResult,
        min_buy: float = 0.0,
        *,
        min_sell: Optional[float] = None,
        sell_5m_min_corr: float = -1.0,
        corr_1h: Optional[float] = None,
        yes_price: float = 0.50,
    ) -> Tuple[float, float]:
        """SOL-style 5m path -- matches sol_macro.py 5m updown.

        Omits live-only lag/spike and correlation dampening, but keeps the
        signal math that decides whether the 5m quant path can clear min_edge.
        """
        ms = float(min_buy) if min_sell is None else float(min_sell)

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

        if allowed_side == "SHORT" and sell_5m_min_corr >= 0:
            if corr_1h is None or corr_1h < sell_5m_min_corr:
                return 0.0, 0.0

        m5_floor = min_buy if allowed_side == "LONG" else ms
        if m5_adj < m5_floor:
            return 0.0, 0.0

        if allowed_side == "LONG":
            est_prob_up += m5_adj
        else:
            est_prob_up -= m5_adj

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

        if allowed_side == "LONG":
            edge = est_prob_up - yes_price
        else:
            edge = yes_price - est_prob_up
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
        btc_ta: Optional[TechnicalAnalysis],
        allowed_side: str,
        df_5m: pd.DataFrame,
        min_eth_adj: float,
        require_btc_impulse: bool,
        *,
        yes_price: float = 0.50,
    ) -> Tuple[float, float]:
        est_prob_up = 0.50 + (0.04 if allowed_side == "LONG" else -0.04)
        btc_impulse = (
            self._eth_follow_btc_5m_impulse(btc_ta, allowed_side)
            if btc_ta is not None
            else 0.0
        )
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
        if allowed_side == "LONG":
            edge = est_prob_up - yes_price
        else:
            edge = yes_price - est_prob_up
        confidence = max(0.55, min(0.85, 0.50 + abs(btc_impulse) * 1.8 + abs(m5_adj) * 2.0))
        return edge, confidence

    def _edge_15m_eth_follow(
        self,
        eth_ta: TechnicalAnalysis,
        btc_ta: Optional[TechnicalAnalysis],
        allowed_side: str,
        min_eth_adj: float,
        min_btc_hist: float,
        *,
        iql_enabled: bool = False,
        iql_hist_floor: float = 0.03,
        yes_price: float = 0.50,
    ) -> Tuple[float, float]:
        if iql_enabled and not UpdownBacktestEngine._passes_15m_iql_macd(
            eth_ta.macd_15m, allowed_side, iql_hist_floor
        ):
            return 0.0, 0.0
        if btc_ta is not None and not self._eth_follow_btc_15m_impulse_ok(
            btc_ta, allowed_side, min_btc_hist
        ):
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
        if allowed_side == "LONG":
            edge = est_prob_up - yes_price
        else:
            edge = yes_price - est_prob_up
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
        """Mirror live KellySizer, then apply the same exposure tier clamp used live."""
        raw_size = self.kelly_sizer.size_from_edge(
            self._signal_strategy_name,
            bankroll=bankroll,
            edge=edge,
        )
        if raw_size <= 0:
            return 0.0
        tier_floor = self.exposure_min_trade_usd
        tier_cap = self.exposure_full_size if self.exposure_full_size > 0 else self.max_size
        size = min(max(raw_size, tier_floor), tier_cap, self.max_size)
        return round(size, 2)

    @staticmethod
    def _last_1m_close_before(df_1m: pd.DataFrame, t: pd.Timestamp) -> Optional[float]:
        """Spot proxy for oracle basis: last 1m close strictly before *t*.

        Live strategies compare Chainlink vs exchange spot; replay TA's
        ``current_price`` is the last *HTF* close (1h/4h) before *t*, which can
        lag by up to nearly one HTF bar and blows up bogus basis vs oracle.
        """
        if df_1m is None or df_1m.empty or "close" not in df_1m.columns:
            return None
        sub = df_1m.loc[df_1m["open_time"] < t, "close"]
        if sub.empty:
            return None
        return float(sub.iloc[-1])

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
        on_progress: Optional[Callable[[int, int, int, float, pd.Timestamp], None]] = None,
        progress_interval: int = 1000,
        max_seconds: Optional[float] = None,
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
        started_at = time.monotonic()

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
        strategy_cfg_map = {
            "BTC": "bitcoin",
            "SOL": "sol_macro",
            "ETH": "eth_macro",
            "XRP": "xrp_macro",
            "HYPE": "hype_macro",
        }
        strategy_cfg_key = strategy_cfg_map.get(symbol, "sol_macro")
        strategy_cfg = self.config.get("strategies", {}).get(strategy_cfg_key, {})
        self._signal_strategy_name = strategy_cfg_key
        self._active_strategy_cfg = strategy_cfg
        self._entry_eval_delay_sec = self._resolve_entry_eval_delay_sec(
            self.config, strategy_cfg
        )
        skip_counts: Counter[str] = Counter()

        def _bump_skip(reason: str) -> None:
            skip_counts[reason] += 1

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

        # BTC uses 4h HTF candles; non-BTC crypto uses each alt's 1h HTF.
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

        data = self._prepare_replay_data_frames(data)
        if btc_data is not None:
            btc_data = self._prepare_replay_data_frames(btc_data)

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

        total_windows = UpdownBacktestResult._count_windows_for_range(
            start_date, end_date, window_minutes
        )
        progress_interval = max(1, int(progress_interval or 1000))

        while current <= end_ts:
            if max_seconds and (time.monotonic() - started_at) >= float(max_seconds):
                logger.warning(
                    "Backtest max_seconds=%.1f reached at %s after %s/%s windows; returning partial result",
                    float(max_seconds),
                    current,
                    windows_scanned,
                    total_windows,
                )
                break
            window_open  = current
            window_close = current + step_td
            windows_scanned += 1
            if on_progress and windows_scanned % progress_interval == 0:
                on_progress(windows_scanned, total_windows, len(trades), bankroll, window_open)

            # Build TechnicalAnalysis from data strictly before this window
            ta = self._build_ta(
                window_open,
                data,
                htf_key,
                include_trend_sabre=is_btc,
            )
            if ta is None:
                _bump_skip("ta_unavailable")
                current += step_td
                continue

            # Also get recent alt slices used by replay-side direction resolution.
            df_1h = self._before(data["1h"], window_open) if "1h" in data else pd.DataFrame()
            df_15m = self._before(data["15m"], window_open)
            df_5m = self._before(data["5m"], window_open)
            df_1m_full = data.get("1m", pd.DataFrame())

            # ==================================================================
            # Layer 1: HTF bias (symbol-specific)
            # ==================================================================
            if is_btc:
                htf_bias = self._get_htf_bias(ta, min_hist=self.min_4h_hist_magnitude)
                btc_ta = ta
            else:
                btc_ta = None
                if btc_data:
                    btc_ta = self._build_ta(
                        window_open,
                        btc_data,
                        "4h",
                        include_trend_sabre=False,
                    )
            if is_eth:
                if self.eth_btc_follow_1h_required and not btc_data:
                    _bump_skip("btc_data_unavailable")
                    current += step_td
                    continue
                if btc_data and btc_ta is None and self.eth_btc_follow_1h_required:
                    _bump_skip("btc_ta_unavailable")
                    current += step_td
                    continue
                direction = self._resolve_alt_replay_direction(
                    symbol=symbol,
                    ta=ta,
                    df_1h=df_1h,
                    df_15m=df_15m,
                    df_5m=df_5m,
                    alt_1m=df_1m_full,
                    btc_ta=btc_ta,
                    btc_data=btc_data,
                    window_open=window_open,
                    strategy_cfg=strategy_cfg,
                )
                if not direction.allowed_side:
                    _bump_skip(direction.skip_reason or "htf_neutral")
                    current += step_td
                    continue
                htf_bias = direction.primary_htf_bias
                allowed_side = direction.allowed_side
            elif not is_btc:
                direction = self._resolve_alt_replay_direction(
                    symbol=symbol,
                    ta=ta,
                    df_1h=df_1h,
                    df_15m=df_15m,
                    df_5m=df_5m,
                    alt_1m=df_1m_full,
                    btc_ta=btc_ta,
                    btc_data=btc_data,
                    window_open=window_open,
                    strategy_cfg=strategy_cfg,
                )
                if not direction.allowed_side:
                    _bump_skip(direction.skip_reason or "htf_neutral")
                    current += step_td
                    continue
                htf_bias = direction.primary_htf_bias
                allowed_side = direction.allowed_side

            if is_btc and htf_bias == "NEUTRAL":
                _bump_skip("htf_neutral")
                current += step_td
                continue

            if is_btc:
                allowed_side = "LONG" if htf_bias == "BULLISH" else "SHORT"
            if window_minutes == 5:
                tf_label = "5m"
            elif window_minutes >= 45:
                tf_label = "1h"
            else:
                tf_label = "15m"
            eval_left_open = self._evaluation_minutes_left_at_open(
                window_minutes, strategy_cfg
            )
            timing_window_open = self._within_entry_timing_window(
                mins_left=eval_left_open,
                tf=tf_label,
            )
            # Live strategies compute a preferred timing band for longer-window markets, but do not
            # hard-reject the entire 30m/1h lane on that signal alone. Keep the strict gate
            # for 5m/15m parity where live skip telemetry showed it mattered most.
            if (
                window_minutes in (5, 15)
                and self._entry_timing_window_is_configured(
                    tf=tf_label, strategy_cfg=strategy_cfg
                )
                and not timing_window_open
            ):
                _bump_skip("timing_window_closed")
                current += step_td
                continue

            if (
                is_eth
                and self.eth_btc_follow_1h_required
                and not direction.skip_btc_follow_1h
                and not self._eth_follow_1h_ok(
                btc_ta, allowed_side, float(self.config.get("strategies", {}).get("eth_macro", {}).get("btc_follow_1h_hist_min", 8.0))
                )
            ):
                _bump_skip("btc_follow_1h")
                current += step_td
                continue

            # ==================================================================
            # Layer 2: LTF confirmation (symbol-specific weights + threshold)
            # ==================================================================
            if is_btc:
                ltf_confirmed, ltf_str = self._ltf_strength(ta, allowed_side)
            else:
                ltf_confirmed, ltf_str = self._sol_ltf_strength(ta, allowed_side)

            # LTF gate policy parity with live strategy config.
            # Default remains anti-LTF (skip confirmed entries), but strategy-level
            # settings can flip to requiring confirmation for noisier assets.
            require_ltf_confirmation = bool(strategy_cfg.get("require_ltf_confirmation", False))
            anti_ltf_gate_enabled = bool(strategy_cfg.get("anti_ltf_gate_enabled", True))
            if require_ltf_confirmation:
                if not ltf_confirmed:
                    _bump_skip("ltf_unconfirmed")
                    current += step_td
                    continue
            else:
                # ETH 15m BTC-follow intentionally allows confirmed follow-through
                # unless the strategy config explicitly enables anti-LTF.
                skip_confirmed = anti_ltf_gate_enabled and not (is_eth and window_minutes != 5)
                if ltf_confirmed and skip_confirmed:
                    _bump_skip("ltf_confirmed")
                    current += step_td
                    continue

            # Determine action (aligned with live: short side buys NO)
            action = "BUY_YES" if allowed_side == "LONG" else "BUY_NO"
            lane_policy = self._resolve_replay_lane_entry_policy(
                symbol=symbol,
                strategy_name=strategy_cfg_key,
                strategy_cfg=strategy_cfg,
                window_minutes=window_minutes,
                action=action,
                min_edge=min_edge,
            )
            if not lane_policy.enabled:
                _bump_skip("lane_disabled")
                current += step_td
                continue
            eval_left = self._replay_eval_minutes_left(
                window_minutes=window_minutes,
                lane_policy=lane_policy,
                strategy_cfg=strategy_cfg,
            )
            if eval_left < lane_policy.entry_window_min or eval_left > lane_policy.entry_window_max:
                _bump_skip("outside_entry_window")
                current += step_td
                continue

            pm_yes = try_load_yes_series_for_window(
                symbol=symbol,
                window_open=window_open,
                window_close=window_close,
                window_minutes=window_minutes,
                cache_root=self._pm_marks_cache_root,
                enabled=self._pm_marks_enabled,
            )
            yes_mid_market = self._yes_mid_at_eval(
                window_open=window_open,
                window_close=window_close,
                window_minutes=window_minutes,
                df_1m=df_1m_full,
                pm_yes=pm_yes,
                eval_minutes_left=eval_left,
            )

            if is_btc and (yes_mid_market < 0.20 or yes_mid_market > 0.80):
                _bump_skip("price_too_far_from_50_50")
                current += step_td
                continue

            raw_est_prob_up = 0.0
            lane_id = ""

            if window_minutes == 5:
                if is_eth:
                    eth_cfg = self.config.get("strategies", {}).get("eth_macro", {})
                    edge, confidence = self._edge_5m_eth_follow_from_df(
                        ta,
                        btc_ta,
                        allowed_side,
                        df_5m,
                        float(eth_cfg.get("eth_follow_5m_min_adj", 0.04)),
                        bool(eth_cfg.get("btc_follow_5m_requires_impulse", True)),
                        yes_price=yes_mid_market,
                    )
                else:
                    corr_1h = None
                    strat_macro = strategy_cfg_map.get(symbol, "sol_macro")
                    sc_macro = self.config.get("strategies", {}).get(strat_macro, {})
                    need_corr = (
                        float(sc_macro.get("sell_5m_min_corr", -1.0)) >= 0
                        and btc_data is not None
                        and btc_data.get("1m") is not None
                        and not btc_data["1m"].empty
                        and df_1m_full is not None
                        and not df_1m_full.empty
                    )
                    if need_corr:
                        corr_1h = self._btc_alt_corr_1h_approx(
                            window_open, btc_data["1m"], df_1m_full
                        )
                    if is_btc:
                        edge, confidence, raw_est_prob_up, lane_id = self._edge_5m_btc(
                            ta,
                            allowed_side,
                            df_1m_full,
                            window_open,
                            ta.macd_4h,
                            yes_price=yes_mid_market,
                            eval_minutes_left=eval_left,
                            window_minutes=window_minutes,
                            htf_bias=htf_bias,
                        )
                    else:
                        edge, confidence = self._edge_5m(
                            ta,
                            allowed_side,
                            df_5m,
                            symbol,
                            df_1m=df_1m_full,
                            window_open=window_open,
                            corr_1h=corr_1h,
                            yes_price=yes_mid_market,
                            eval_minutes_left=eval_left,
                            window_minutes=window_minutes,
                            htf_bias=htf_bias,
                        )
            else:
                if is_btc:
                    raw_est_prob_up, confidence = self._edge_15m(
                        ta, allowed_side, ltf_str, htf_bias, yes_mid_market
                    )
                    edge, lane_id, _cal_p = edge_from_raw_est_prob(
                        self._lane_calibrator,
                        raw_est_prob_up,
                        yes_mid_market,
                        allowed_side,
                        strategy="bitcoin",
                        window_minutes=window_minutes,
                        htf_bias=htf_bias,
                        signal_reason=f"UPDOWN_{window_minutes}m",
                    )
                elif is_eth:
                    eth_cfg = self.config.get("strategies", {}).get("eth_macro", {})
                    edge, confidence = self._edge_15m_eth_follow(
                        ta,
                        btc_ta,
                        allowed_side,
                        float(eth_cfg.get("eth_follow_15m_min_adj", 0.04)),
                        float(eth_cfg.get("btc_follow_15m_hist_min", 0.03)),
                        iql_enabled=bool(eth_cfg.get("iql_15m_enabled", False)),
                        iql_hist_floor=float(eth_cfg.get("iql_15m_hist_floor", 0.03)),
                        yes_price=yes_mid_market,
                    )
                else:
                    strat_macro = strategy_cfg_map.get(symbol, "sol_macro")
                    sc_macro = self.config.get("strategies", {}).get(strat_macro, {})
                    edge, confidence = self._edge_15m_sol(
                        ta,
                        allowed_side,
                        ltf_str,
                        iql_enabled=bool(sc_macro.get("iql_15m_enabled", False)),
                        iql_hist_floor=float(sc_macro.get("iql_15m_hist_floor", 0.03)),
                        yes_price=yes_mid_market,
                    )
            effective_min_edge = max(lane_policy.min_edge, lane_policy.hard_min_edge)
            if symbol == "HYPE":
                hard_min_edge = max(0.05, float(strategy_cfg.get("hard_min_edge", 0.05)))
                hard_min_edge *= self._btc_1h_regime_min_edge_mult(strategy_cfg, btc_ta)
                hard_min_edge *= get_drift_min_edge_mult("hype_macro", self.config)
                effective_min_edge = max(effective_min_edge, hard_min_edge)

            # Min edge filter
            if edge < effective_min_edge:
                _bump_skip("edge_below_min")
                if symbol == "HYPE" and effective_min_edge > min_edge:
                    _bump_skip("hard_min_edge")
                current += step_td
                continue

            oracle_price = self._oracle_price_at(oracle_times_ns, oracle_prices, window_open)
            if oracle_max_basis_bps is not None and oracle_price and oracle_price > 0:
                spot_basis = self._last_1m_close_before(data.get("1m", pd.DataFrame()), window_open)
                spot_for_basis = spot_basis if spot_basis is not None else float(ta.current_price)
                basis_bps = ((spot_for_basis - oracle_price) / oracle_price) * 10000.0
                basis_relax_max_bps = strategy_cfg.get("oracle_basis_relax_max_bps")
                allowed_basis_bps = (
                    float(basis_relax_max_bps)
                    if basis_relax_max_bps is not None
                    else float(oracle_max_basis_bps)
                )
                if abs(basis_bps) > allowed_basis_bps:
                    oracle_basis_skips += 1
                    _bump_skip("oracle_basis")
                    current += step_td
                    continue

            max_edge_updown = float(
                strategy_cfg.get(
                    f"max_edge_updown_{tf_label}_{allowed_side.lower()}",
                    strategy_cfg.get(
                        f"max_edge_updown_{tf_label}",
                        strategy_cfg.get(
                            f"max_edge_updown_{allowed_side.lower()}",
                            strategy_cfg.get(
                                "max_edge_updown",
                                self.config.get("max_edge_updown", 0.0),
                            ),
                        ),
                    ),
                )
                or 0.0
            )
            sizing_edge = (
                min(edge, max_edge_updown)
                if max_edge_updown > 0 and edge > max_edge_updown
                else edge
            )

            # Position size
            size = self._size_position(bankroll, sizing_edge) * max(0.0, lane_policy.size_multiplier)
            if size <= 0 or bankroll < size:
                _bump_skip("size_rejected")
                current += step_td
                continue
            size = round(size, 2)

            # Fill at realistic mid-price: use empirical distribution from live fills
            # when available (>=20 recorded), else N(0.50, 0.06) clipped to [0.30, 0.70].
            mid_price = self._sample_entry_price()
            if action == "BUY_YES":
                entry_price_bad = (
                    yes_mid_market < lane_policy.entry_price_min
                    or yes_mid_market > lane_policy.entry_price_max
                )
            else:
                entry_price_bad = yes_mid_market < lane_policy.entry_price_min
            if entry_price_bad:
                _bump_skip("entry_price_band")
                current += step_td
                continue
            center_price_band = float(strategy_cfg.get("center_price_band", 0.0) or 0.0)
            min_edge_when_centered = float(
                strategy_cfg.get("min_edge_when_centered", effective_min_edge)
            )
            if center_price_band > 0 and abs(yes_mid_market - 0.50) <= center_price_band:
                centered_min = max(effective_min_edge, min_edge_when_centered)
                if edge < centered_min:
                    _bump_skip("centered_price_edge_below_min")
                    current += step_td
                    continue
            trade_mid = mid_price if action == "BUY_YES" else max(
                0.01, min(0.99, 1.0 - mid_price)
            )
            fill_price, slip_cost = self._simulate_fill(trade_mid, "BUY")
            slippage_total += slip_cost * size

            # Replay live-like exits from 1m data for the window, then settle if needed.
            df_1m = data.get("1m", pd.DataFrame())
            if df_1m.empty or "open_time" not in df_1m.columns:
                _bump_skip("entry_1m_missing")
                current += step_td
                continue
            window_df_1m = df_1m[
                (df_1m["open_time"] >= window_open)
                & (df_1m["open_time"] < window_close)
            ].copy()
            asset_open_px = float(window_df_1m.iloc[0]["open"]) if not window_df_1m.empty else 0.0
            pnl, outcome, exit_price, asset_open, asset_close, exit_reason = (
                self._settle_updown_with_live_exit_proxy(
                    df_1m=df_1m,
                    window_open=window_open,
                    window_close=window_close,
                    action=action,
                    entry_price=mid_price,
                    size=size,
                    asset_open=asset_open_px,
                    fill_price=fill_price,
                    symbol=symbol,
                    window_minutes=window_minutes,
                    pm_yes=pm_yes,
                )
            )
            if not outcome:
                # Cannot settle this window (no 1m data) -- skip
                _bump_skip("unsettled_window")
                current += step_td
                continue

            bankroll = max(0.0, bankroll + pnl)   # ruin cap

            if is_btc and lane_id:
                record_updown_calibration_close(
                    self._lane_calibrator,
                    lane_id=lane_id,
                    stated_est_prob=raw_est_prob_up if raw_est_prob_up else None,
                    pnl=pnl,
                    size=size,
                    outcome=outcome,
                )

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
                exit_reason=exit_reason,
                raw_est_prob_up=raw_est_prob_up,
                lane_id=lane_id,
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
            replay_assumptions=self._build_replay_assumptions(symbol, window_minutes),
            skip_counts=dict(skip_counts),
            total_windows=total_windows,
            run_complete=windows_scanned >= total_windows,
            elapsed_seconds=round(time.monotonic() - started_at, 3),
        )
