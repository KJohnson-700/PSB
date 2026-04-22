import logging
from typing import List, Dict, Any
from src.market.scanner import Market
from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.fade_models import FadeSignal

class FadeStrategy:
    def __init__(self, config: Dict[str, Any], ai_agent: AIAgent, position_sizer: PositionSizer,
                 kelly_sizer=None):
        self.config = config.get('strategies', {}).get('fade', {})
        self.ai_agent = ai_agent
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer
        self._signal_strategy_name = "fade"
        self.consensus_threshold = self.config.get('consensus_threshold', 0.95)
        self.consensus_threshold_lower = self.config.get('consensus_threshold_lower', 0.80)
        self.consensus_threshold_upper = self.config.get('consensus_threshold_upper', 0.95)
        self.ai_confidence_threshold = self.config.get('ai_confidence_threshold', 0.60)
        self.ipg_min = self.config.get('ipg_min', 0.10)
        self.kelly_fraction = self.config.get('kelly_fraction', 0.10)
        self.entry_price_min = self.config.get('entry_price_min', 0.15)
        self.entry_price_max = self.config.get('entry_price_max', 0.45)
        self.enabled = self.config.get('enabled', False)
        logging.debug("FadeStrategy initialized")

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[FadeSignal]:
        """
        Scans for markets with extreme consensus and analyzes them for fade opportunities.

        Smart AI gating: Only calls AI when the consensus is in the "ambiguous" range
        (0.80-0.90). For extreme consensus (>= 0.90), the edge is clear enough to fade
        without burning an AI call — we use a synthetic probability estimate instead.
        """
        if not self.enabled:
            return []

        # Legal/court outcome markets are highly binary and hard to fade via consensus.
        # Live data: Weinstein legal market = -$2.23 (worst single fade loss).
        _LEGAL_KEYWORDS = ['sentenced', 'sentencing', 'prison', 'verdict', 'convicted',
                           'acquitted', 'guilty', 'charges', 'indicted', 'arrested']

        signals = []
        for market in markets:
            # Skip legal/court markets — binary outcomes defy consensus fade logic
            q_lower = market.question.lower() if hasattr(market, 'question') else ''
            if any(kw in q_lower for kw in _LEGAL_KEYWORDS):
                logging.debug(f"Fade: skipping legal market '{market.question[:50]}'")
                continue

            # Skip short-window candle markets (BTC/SOL Up or Down 5m/15m)
            # These are already covered by bitcoin/sol_lag strategies.
            # Double-entering the same market doubles losses when both strategies are wrong.
            if "up or down" in q_lower and any(
                sym in q_lower for sym in ("bitcoin", "solana", "ethereum")
            ):
                logging.debug(f"Fade: skipping candle market '{market.question[:50]}'")
                continue

            # Find markets with consensus in range (lower-upper; avoid lottery zone <0.15 for opposite)
            yes_high = market.yes_price >= self.consensus_threshold_lower and market.yes_price <= self.consensus_threshold_upper
            no_high = market.no_price >= self.consensus_threshold_lower and market.no_price <= self.consensus_threshold_upper
            if not (yes_high or no_high):
                continue

            # Determine which side has the consensus
            consensus_side = 'YES' if market.yes_price > market.no_price else 'NO'
            consensus_price = market.yes_price if consensus_side == 'YES' else market.no_price

            # --- Smart AI Gating ---
            # If consensus is extreme (>= 0.90), the edge is clear: fade it without AI.
            # If consensus is moderate (0.80-0.90), it's ambiguous: call AI to confirm.
            CLEAR_EDGE_THRESHOLD = 0.90

            if consensus_price >= CLEAR_EDGE_THRESHOLD:
                # Clear edge (>=0.90) — synthetic estimate, no AI needed.
                # Market is priced at near-certainty; historical base rate says
                # ~15% discount is conservative and captures the fade edge.
                ai_true_prob = consensus_price - 0.15
                ai_confidence = 0.65
                implied_probability_gap = abs(consensus_price - ai_true_prob)
                logging.debug(
                    f"Fade: Clear edge on '{market.question[:40]}...' — "
                    f"consensus={consensus_price:.2f}, synthetic_prob={ai_true_prob:.2f}, IPG={implied_probability_gap:.2f}"
                )
            else:
                # Ambiguous range (0.80–0.90).
                # If use_ai + AI is available use it; otherwise fall back to a conservative
                # synthetic estimate so the strategy keeps generating signals.
                if self.config.get("use_ai", True) and self.ai_agent.is_available():
                    ai_analysis = await self.ai_agent.analyze_market(
                        market_question=market.question,
                        market_description=market.description,
                        current_yes_price=market.yes_price,
                        market_id=market.id
                    )
                    if not ai_analysis:
                        continue
                    if ai_analysis.recommendation == "HOLD":
                        logging.debug(f"Fade: AI recommends HOLD for '{market.question[:40]}...' — skipping")
                        continue
                    ai_true_prob = ai_analysis.estimated_probability
                    ai_confidence = ai_analysis.confidence_score
                else:
                    # Pure-quant fallback: assume ~10% overpricing in ambiguous zone
                    # (slightly more conservative than the >=0.90 clear-edge 15% discount).
                    ai_true_prob = consensus_price - 0.10
                    ai_confidence = 0.62
                    logging.debug(
                        f"Fade: AI offline — quant fallback for '{market.question[:40]}...' "
                        f"consensus={consensus_price:.2f}, synthetic_prob={ai_true_prob:.2f}"
                    )
                implied_probability_gap = abs(consensus_price - ai_true_prob)

            # Check for signal criteria
            if ai_confidence >= self.ai_confidence_threshold and implied_probability_gap >= self.ipg_min:
                # Determine action (bet against consensus)
                action = "SELL_YES" if consensus_side == 'YES' else "BUY_YES"
                # Entry price filter: side we're buying must be in 0.15-0.45 (avoid lottery <0.2)
                entry_price = market.yes_price if action == "BUY_YES" else market.no_price
                if entry_price < self.entry_price_min or entry_price > self.entry_price_max:
                    continue

                # Calculate position size
                edge = implied_probability_gap - self.config.get('fee_buffer', 0.02)
                size = self.kelly_sizer.size_from_edge(
                    self._signal_strategy_name, bankroll, edge
                ) if self.kelly_sizer else self.position_sizer.calculate_kelly_bet(
                    bankroll, edge, self.kelly_fraction
                )

                # Hard cap: never exceed max_trade_size_pct of bankroll per trade
                max_trade_size_pct = self.config.get('max_trade_size_pct', 0.02)
                size = min(size, bankroll * max_trade_size_pct)

                # Secondary guard for SELL_YES: cap dollar risk to 3% of bankroll.
                # When selling YES at a high price, dollar risk = size * yes_price
                # (if YES wins, you lose size * yes_price). This prevents outsized
                # losses on near-certain markets like YES=0.939.
                if action == "SELL_YES":
                    max_sell_yes_risk = bankroll * 0.03
                    yes_price = market.yes_price
                    if yes_price > 0 and size * yes_price > max_sell_yes_risk:
                        size = max_sell_yes_risk / yes_price

                if size > 0:
                    order_price = market.yes_price - 0.01 if action == "SELL_YES" else market.yes_price + 0.01
                    order_price = max(0.01, min(0.99, order_price))

                    signal = FadeSignal(
                        market_id=market.id,
                        token_id_yes=market.token_id_yes,
                        token_id_no=market.token_id_no,
                        market_question=market.question,
                        end_date=market.end_date,
                        action=action,
                        consensus_price=consensus_price,
                        ai_confidence=ai_confidence,
                        implied_probability_gap=implied_probability_gap,
                        size=size,
                        price=order_price
                    )
                    signals.append(signal)

        if signals:
            logging.info("Fade: Generated %d signals (AI called only for ambiguous cases).", len(signals))

        return signals
