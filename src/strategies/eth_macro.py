"""
ETH Macro Strategy — BTC-follow execution for ETH up/down markets.

Design:
- BTC 4H decides regime.
- BTC 1H confirms continuation quality.
- BTC short-window momentum triggers the follow setup.
- ETH 5m/15m momentum only confirms follow-through.
"""
import logging
import time
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.analysis.ai_agent import AIAgent
from src.analysis.btc_price_service import BTCPriceService, CandleMomentum, MACDResult, TechnicalAnalysis
from src.analysis.math_utils import PositionSizer
from src.analysis.sol_btc_service import SOLBTCService
from src.execution.exposure_manager import ExposureManager, ExposureTier
from src.market.scanner import Market
from src.strategies.strategy_config import resolve_enabled_flag
from src.strategies.sol_macro import SolMacroSignal, SolMacroStrategy
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)

logger = logging.getLogger(__name__)

ETH_PATTERNS = [
    re.compile(r"\bethereum\b", re.IGNORECASE),
    re.compile(r"\beth\b", re.IGNORECASE),
    re.compile(r"\bether\b", re.IGNORECASE),
]
ETH_UPDOWN_PATTERN = re.compile(
    r"(?:ethereum|eth|ether)\s+up\s+or\s+down", re.IGNORECASE
)


class ETHMacroStrategy(SolMacroStrategy):
    """ETH strategy using BTC-follow regime logic instead of SOL lag logic."""

    def _build_alt_service(self) -> SOLBTCService:
        return SOLBTCService(
            alt_symbol="ETHUSDT",
            dynamic_beta_min=self.dynamic_beta_min,
            dynamic_beta_max=self.dynamic_beta_max,
            dynamic_beta_extreme_max=self.dynamic_beta_extreme_max,
            btc_spike_floor_pct_5m=self.btc_spike_floor_pct_5m,
            btc_spike_floor_pct_15m=self.btc_spike_floor_pct_15m,
            lag_signal_min_pct=self.lag_signal_min_pct,
        )

    def __init__(
        self,
        config: Dict[str, Any],
        ai_agent: AIAgent,
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: ExposureManager = None,
    ):
        super().__init__(config, ai_agent, position_sizer, kelly_sizer, exposure_manager)
        self.config = config.get("strategies", {}).get("eth_macro", {})
        self.enabled = resolve_enabled_flag(
            "eth_macro",
            self.config,
            logger=logger,
        )
        self._apply_strategy_config(rebuild_service=True)
        self.btc_service = BTCPriceService()
        self._signal_strategy_name = "eth_macro"

        self.btc_follow_1h_hist_min = float(self.config.get("btc_follow_1h_hist_min", 8.0))
        self.btc_follow_15m_hist_min = float(self.config.get("btc_follow_15m_hist_min", 0.03))
        self.btc_follow_5m_requires_impulse = bool(
            self.config.get("btc_follow_5m_requires_impulse", True)
        )
        self.eth_follow_5m_min_adj = float(self.config.get("eth_follow_5m_min_adj", 0.04))
        self.eth_follow_15m_hist_min = float(self.config.get("eth_follow_15m_hist_min", 0.03))
        self.eth_follow_15m_min_adj = float(self.config.get("eth_follow_15m_min_adj", 0.04))
        self.ai_hold_veto_ttl_sec = self.config.get("ai_hold_veto_ttl_sec", 300)
        self.min_edge_5m_ai_override = self.config.get("min_edge_5m_ai_override", 0.10)

    def _is_solana_market(self, market: Market) -> bool:
        text = f"{market.question} {market.description}".lower()
        has_eth = any(p.search(text) for p in ETH_PATTERNS)
        is_btc_only = "bitcoin" in text and not has_eth
        return has_eth and not is_btc_only

    def _is_updown_market(self, market: Market) -> bool:
        return bool(ETH_UPDOWN_PATTERN.search(market.question))

    def _btc_follow_1h_ok(self, btc_ta: TechnicalAnalysis, allowed_side: str) -> bool:
        macd_1h = btc_ta.macd_1h
        min_hist = self.btc_follow_1h_hist_min
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

    def _btc_follow_15m_impulse_ok(self, btc_ta: TechnicalAnalysis, allowed_side: str) -> bool:
        macd_15m = btc_ta.macd_15m
        min_hist = self.btc_follow_15m_hist_min
        direction = btc_ta.candle_momentum.m15_direction
        if allowed_side == "LONG":
            return (
                macd_15m.crossover == "BULLISH_CROSS"
                or (
                    macd_15m.histogram > min_hist
                    and macd_15m.histogram_rising
                    and direction in ("SPIKE_UP", "DRIFT_UP")
                )
            )
        return (
            macd_15m.crossover == "BEARISH_CROSS"
            or (
                macd_15m.histogram < -min_hist
                and not macd_15m.histogram_rising
                and direction in ("SPIKE_DOWN", "DRIFT_DOWN")
            )
        )

    def _btc_follow_5m_impulse_score(self, momentum: CandleMomentum, allowed_side: str) -> tuple[float, List[str]]:
        direction = momentum.m5_direction
        reasons: List[str] = []
        score = 0.0
        if allowed_side == "LONG":
            if direction == "SPIKE_UP":
                score = 0.06
                reasons.append(f"BTC5m SPIKE_UP ({momentum.m5_move_pct:+.3f}%)")
            elif direction == "DRIFT_UP":
                score = 0.04
                reasons.append(f"BTC5m DRIFT_UP ({momentum.m5_move_pct:+.3f}%)")
            elif direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                score = -0.05
                reasons.append(f"BTC5m against ({direction})")
        else:
            if direction == "SPIKE_DOWN":
                score = 0.06
                reasons.append(f"BTC5m SPIKE_DOWN ({momentum.m5_move_pct:+.3f}%)")
            elif direction == "DRIFT_DOWN":
                score = 0.04
                reasons.append(f"BTC5m DRIFT_DOWN ({momentum.m5_move_pct:+.3f}%)")
            elif direction in ("SPIKE_UP", "DRIFT_UP"):
                score = -0.05
                reasons.append(f"BTC5m against ({direction})")
        if momentum.m5_in_prediction_window and score > 0:
            score += 0.02
            reasons.append("BTC5m predict window")
        return score, reasons

    @staticmethod
    def _eth_5m_macd_score(macd_5m: MACDResult, allowed_side: str) -> tuple[float, List[str]]:
        reasons: List[str] = []
        score = 0.0
        if allowed_side == "LONG":
            if macd_5m.crossover == "BULLISH_CROSS":
                score = 0.06
                reasons.append("ETH5m bull cross")
            elif macd_5m.histogram > 0 and macd_5m.histogram_rising:
                score = 0.04
                reasons.append("ETH5m green+rising")
            elif macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
                score = -0.05
                reasons.append("ETH5m against")
        else:
            if macd_5m.crossover == "BEARISH_CROSS":
                score = 0.06
                reasons.append("ETH5m bear cross")
            elif macd_5m.histogram < 0 and not macd_5m.histogram_rising:
                score = 0.04
                reasons.append("ETH5m red+falling")
            elif macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
                score = -0.05
                reasons.append("ETH5m against")
        return score, reasons

    def _eth_15m_follow_score(self, macd_15m: MACDResult, allowed_side: str) -> tuple[float, List[str]]:
        reasons: List[str] = []
        score = 0.0
        min_hist = self.eth_follow_15m_hist_min
        if allowed_side == "LONG":
            if macd_15m.crossover == "BULLISH_CROSS":
                score = 0.06
                reasons.append("ETH15m bull cross")
            elif macd_15m.histogram >= min_hist and macd_15m.histogram_rising:
                score = 0.05
                reasons.append(f"ETH15m green+rising>{min_hist:.2f}")
            elif macd_15m.crossover == "BEARISH_CROSS" or macd_15m.histogram < 0:
                score = -0.05
                reasons.append("ETH15m against")
        else:
            if macd_15m.crossover == "BEARISH_CROSS":
                score = 0.06
                reasons.append("ETH15m bear cross")
            elif macd_15m.histogram <= -min_hist and not macd_15m.histogram_rising:
                score = 0.05
                reasons.append(f"ETH15m red+falling>{min_hist:.2f}")
            elif macd_15m.crossover == "BULLISH_CROSS" or macd_15m.histogram > 0:
                score = -0.05
                reasons.append("ETH15m against")
        return score, reasons

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[SolMacroSignal]:
        if not self.enabled:
            return []

        eth_markets = [m for m in markets if self._is_solana_market(m) and self._is_updown_market(m)]
        if not eth_markets:
            logger.info("ETH Macro strategy: 0 ETH updown markets found")
            return []

        eth_ta = self.sol_service.get_full_analysis()
        btc_ta = self.btc_service.get_full_analysis()
        if not eth_ta or not btc_ta:
            logger.warning("ETH Macro strategy: BTC or ETH analysis unavailable")
            return []

        conditions = self.conditions_from_ta(eth_ta)
        exp_tier, exp_multiplier, _exp_max_size, exp_reason = self.exposure_manager.get_exposure(conditions)
        if exp_tier == ExposureTier.PAUSED:
            logger.info(f"ETH Macro strategy: PAUSED — {exp_reason}")
            return []

        btc_htf_bias = self._get_btc_htf_bias(btc_ta)
        if btc_htf_bias == "NEUTRAL":
            logger.info("ETH Macro strategy: BTC HTF neutral — sitting out")
            return []

        allowed_side = "LONG" if btc_htf_bias == "BULLISH" else "SHORT"
        if not self._btc_follow_1h_ok(btc_ta, allowed_side):
            logger.info(
                "ETH Macro strategy: BTC 1H continuation not strong enough "
                f"(bias={btc_htf_bias}, hist={btc_ta.macd_1h.histogram:+.2f})"
            )
            return []

        eth = eth_ta.sol
        eth_price = eth.current_price
        btc_mom = btc_ta.candle_momentum
        mtt = eth_ta.multi_tf

        logger.info(
            f"ETH ${eth_price:,.2f} | BTC_HTF={btc_htf_bias} | BTC1H hist={btc_ta.macd_1h.histogram:+.2f} "
            f"BTC15m={btc_ta.macd_15m.histogram:+.3f} BTC5m={btc_mom.m5_direction}({btc_mom.m5_move_pct:+.3f}%) "
            f"| ETH15m={eth.macd_15m.histogram:+.3f} {eth.macd_15m.crossover} "
            f"| ETH5m={eth.macd_5m.histogram:+.3f} {eth.macd_5m.crossover} | RSI={eth.rsi_14:.0f}"
        )

        signals: List[SolMacroSignal] = []
        ai_calls = 0
        skip_reasons: Dict[str, int] = {}
        gate_samples: Dict[str, list] = {}

        def _bump_skip(reason: str) -> None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        def _sample(metric: str, value) -> None:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return
            if not (v == v):  # NaN check
                return
            gate_samples.setdefault(metric, []).append(v)

        def _summarize(values: list) -> dict:
            if not values:
                return {}
            vs = sorted(values)
            n = len(vs)
            def pct(p):
                idx = max(0, min(n - 1, int(round((n - 1) * p))))
                return round(vs[idx], 4)
            return {"n": n, "min": round(vs[0], 4), "p25": pct(0.25), "p50": pct(0.50), "p75": pct(0.75), "max": round(vs[-1], 4)}

        for market in eth_markets:
            if market.liquidity > 0 and market.liquidity < self.min_liquidity:
                _bump_skip("liquidity")
                continue

            is_5m = self._is_5m_market(market)
            yes_price = market.yes_price
            if yes_price < 0.20 or yes_price > 0.80:
                _bump_skip("price_too_far")
                continue

            if not market.end_date:
                _bump_skip("no_end_date")
                continue

            _end_utc = (
                market.end_date.replace(tzinfo=timezone.utc)
                if market.end_date.tzinfo is None else market.end_date
            )
            _mins_left = (_end_utc - datetime.now(timezone.utc)).total_seconds() / 60.0
            if is_5m:
                _win_min, _win_max = self._resolve_entry_window_bounds(
                    is_5m=True, default_min=2.5, default_max=4.0
                )
            else:
                _win_min, _win_max = self._resolve_entry_window_bounds(
                    is_5m=False, default_min=12.0, default_max=14.5
                )
            _sample("mins_left", _mins_left)
            if _mins_left < _win_min or _mins_left > _win_max:
                _bump_skip("outside_window")
                continue
            _sample("entry_price", yes_price)
            _ai_window_open = self._within_ai_decision_window(
                mins_left=_mins_left,
                is_5m=is_5m,
            )

            action = "BUY_YES" if allowed_side == "LONG" else "SELL_YES"
            direction = "UP" if allowed_side == "LONG" else "DOWN"

            if action == "SELL_YES" and mtt.h1_trend == "BULLISH":
                _bump_skip("eth_1h_bullish")
                continue
            if action == "BUY_YES" and mtt.h1_trend == "BEARISH":
                _bump_skip("eth_1h_bearish")
                continue
            if self._rsi_blocks_entry(action, eth.rsi_14):
                _bump_skip("rsi_block")
                continue
            if self._oracle_basis_blocks_entry(eth.oracle_basis_bps):
                _bump_skip("oracle_basis_block")
                continue

            est_prob_up = 0.50
            reason_parts = [f"BTC_HTF={btc_htf_bias}", f"side={allowed_side}"]
            confidence = 0.50
            ai_used = False

            if is_5m:
                btc_impulse, btc_reasons = self._btc_follow_5m_impulse_score(btc_mom, allowed_side)
                if self.btc_follow_5m_requires_impulse and btc_impulse <= 0:
                    _bump_skip("btc_5m_no_impulse")
                    continue
                eth_5m_adj, eth_reasons = self._eth_5m_macd_score(eth.macd_5m, allowed_side)
                if eth_5m_adj < self.eth_follow_5m_min_adj:
                    _bump_skip("eth_5m_weak_confirm")
                    continue
                est_prob_up = self._apply_primary_htf_bias(est_prob_up, btc_htf_bias, 0.04)
                est_prob_up += btc_impulse if allowed_side == "LONG" else -btc_impulse
                est_prob_up += eth_5m_adj if allowed_side == "LONG" else -eth_5m_adj
                if eth.rsi_14 > 75:
                    est_prob_up -= 0.02
                elif eth.rsi_14 < 25:
                    est_prob_up += 0.02
                confidence = max(0.55, min(0.85, 0.50 + abs(btc_impulse) * 1.8 + abs(eth_5m_adj) * 2.0))
                reason_parts.extend(["UPDOWN_5m", *btc_reasons, *eth_reasons])
            else:
                if not self._btc_follow_15m_impulse_ok(btc_ta, allowed_side):
                    _bump_skip("btc_15m_not_following")
                    continue
                eth_15m_adj, eth_reasons = self._eth_15m_follow_score(eth.macd_15m, allowed_side)
                if eth_15m_adj < self.eth_follow_15m_min_adj:
                    _bump_skip("eth_15m_weak_confirm")
                    continue
                est_prob_up = self._apply_primary_htf_bias(est_prob_up, btc_htf_bias, 0.08)
                est_prob_up += eth_15m_adj if allowed_side == "LONG" else -eth_15m_adj
                if eth.rsi_14 > 75:
                    est_prob_up -= 0.03
                elif eth.rsi_14 < 25:
                    est_prob_up += 0.03
                confidence = max(0.55, min(0.85, 0.50 + abs(eth_15m_adj) * 2.2))
                reason_parts.extend(["UPDOWN_15m", *eth_reasons])

            est_prob_up = max(0.10, min(0.90, est_prob_up))
            edge = est_prob_up - yes_price if action == "BUY_YES" else yes_price - est_prob_up
            if edge <= 0:
                _bump_skip("nonpositive_edge")
                continue

            effective_min_edge = self.min_edge_5m if is_5m else self.min_edge
            _hold_ts = self._ai_hold_cache.get(market.id, 0)
            _hold_age = time.time() - _hold_ts
            if _hold_age < self.ai_hold_veto_ttl_sec and edge < self.min_edge_5m_ai_override:
                _bump_skip("ai_hold_veto")
                continue

            if (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and _ai_window_open
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
            ):
                _window = "5m" if is_5m else "15m"
                ai_context = (
                    f"{market.description}\n\n"
                    f"=== ETH BTC-FOLLOW CONTEXT ({_window}) ===\n"
                    f"ETH Price: ${eth_price:,.2f} | YES={yes_price:.3f} | action={action}\n"
                    f"BTC_HTF={btc_htf_bias} | side={allowed_side} | Quant edge={edge:.4f} "
                    f"(threshold={effective_min_edge:.4f})\n"
                    f"Minutes left={_mins_left:.1f}\n\n"
                    f"BTC 1H hist={btc_ta.macd_1h.histogram:+.2f} rising={btc_ta.macd_1h.histogram_rising}\n"
                    f"BTC 15m hist={btc_ta.macd_15m.histogram:+.3f} cross={btc_ta.macd_15m.crossover}\n"
                    f"BTC 5m={btc_mom.m5_direction} ({btc_mom.m5_move_pct:+.3f}%)\n"
                    f"ETH 15m hist={eth.macd_15m.histogram:+.3f} cross={eth.macd_15m.crossover}\n"
                    f"ETH 5m hist={eth.macd_5m.histogram:+.3f} cross={eth.macd_5m.crossover}\n"
                    f"ETH RSI={eth.rsi_14:.1f} | ETH 1H trend={mtt.h1_trend}\n"
                    f"ETH Chainlink={eth.chainlink_price if eth.chainlink_price is not None else 'n/a'} "
                    f"basis_bps={eth.oracle_basis_bps if eth.oracle_basis_bps is not None else 'n/a'}\n\n"
                    f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                    "Answer with BUY_YES, BUY_NO, or HOLD."
                )
                ai_analysis = await self.ai_agent.analyze_market(
                    market_question=market.question,
                    market_description=ai_context,
                    current_yes_price=yes_price,
                    market_id=market.id,
                    strategy_hint=self._signal_strategy_name,
                )
                ai_calls += 1
                ai_used = True
                if not ai_analysis:
                    _bump_skip("ai_none")
                    continue
                if ai_analysis.recommendation == "HOLD":
                    self._ai_hold_cache[market.id] = time.time()
                    _bump_skip("ai_hold")
                    continue
                if not ai_recommendation_supports_action(ai_analysis.recommendation, action):
                    _bump_skip("ai_veto")
                    continue
                if ai_analysis.confidence_score < self.ai_confidence_threshold:
                    _bump_skip("ai_low_confidence")
                    continue
                ai_prob_yes = float(ai_analysis.estimated_probability)
                ai_edge = ai_prob_yes - yes_price if action == "BUY_YES" else yes_price - ai_prob_yes
                if ai_edge <= 0:
                    _bump_skip("ai_nonpositive_edge")
                    continue
                edge = max(edge, ai_edge)
                confidence = max(confidence, ai_analysis.confidence_score)
                reason_parts.append("ai_updown_confirm")
            elif (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _ai_window_open
            ):
                _bump_skip("ai_window_closed")

            _sample("est_prob_up", est_prob_up)
            _sample("edge", edge)
            if edge < effective_min_edge:
                _bump_skip("edge_below_min")
                continue

            if yes_price < self.entry_price_min or yes_price > self.entry_price_max:
                _bump_skip("entry_price_band")
                continue

            max_edge_updown = float(self.config.get("max_edge_updown", 0.15))
            if edge > max_edge_updown:
                _bump_skip("edge_above_cap")
                continue

            if not self.kelly_sizer:
                _bump_skip("kelly_unavailable")
                logger.error("ETH strategy: KellySizer unavailable — skipping entry sizing")
                continue
            raw_size = self.kelly_sizer.size_from_edge(
                self._signal_strategy_name, bankroll, edge
            )
            final_size = self.exposure_manager.scale_size(raw_size)
            if final_size < 0.5:
                _bump_skip("size_too_small")
                continue

            reason_parts.extend([
                f"ETH=${eth_price:,.2f}",
                f"BTC5m={btc_mom.m5_direction}",
                f"est_up={est_prob_up:.3f}",
                f"mkt_yes={yes_price:.3f}",
                f"RSI={eth.rsi_14:.0f}",
                f"oracle_basis={eth.oracle_basis_bps:+.1f}bps" if eth.oracle_basis_bps is not None else "",
                f"exp={exp_tier.value}(x{exp_multiplier:.1f})",
            ])
            signal = SolMacroSignal(
                market_id=market.id,
                market_question=market.question,
                action=action,
                price=yes_price if action == "BUY_YES" else (1 - yes_price),
                size=round(final_size, 2),
                confidence=round(confidence, 3),
                edge=round(edge, 4),
                token_id_yes=market.token_id_yes,
                token_id_no=market.token_id_no,
                end_date=market.end_date,
                direction=direction,
                sol_threshold=None,
                sol_current=round(eth_price, 2),
                btc_current=round(btc_ta.current_price, 2),
                lag_magnitude=None,
                ai_used=ai_used,
                reason=" | ".join(reason_parts),
                strategy_name=self._signal_strategy_name,
                alt_asset_code="eth",
                htf_bias=btc_htf_bias,
                window_size="5m" if is_5m else "15m",
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=round(est_prob_up, 4),
                rsi=round(eth.rsi_14, 1),
                corr_1h=None,
            )
            signals.append(signal)

        if signals:
            logger.info(f"ETH Macro strategy: {len(signals)} signals generated")
        else:
            top_reason = max(skip_reasons, key=skip_reasons.get) if skip_reasons else "no_eligible_markets"
            logger.info(f"ETH Macro strategy: 0 signals (BTC_HTF={btc_htf_bias}, top_skip={top_reason})")
        gate_distributions = {k: _summarize(v) for k, v in gate_samples.items()}
        if gate_samples:
            logger.info(f"  [gate-dist] {gate_distributions}")
        self.last_scan_stats = {
            "enabled": True,
            "signals": len(signals),
            "markets_considered": len(eth_markets),
            "btc_htf_bias": btc_htf_bias,
            "top_skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "gate_distributions": gate_distributions,
        }
        return signals
