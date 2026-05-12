"""Shared decision-core primitives used by both live strategies and the backtest engine.

Pure functions only. No IO, no logging, no config-object dependencies — callers
pass in scalars. The contract: live and backtest call the SAME function with the
SAME inputs and get the SAME output. Any drift between live and backtest must
live in the callers, not here.
"""

from .htf_bias import BtcHtfBiasResult, btc_htf_bias
from .ltf_strength import (
    LtfStrengthResult,
    btc_ltf_strength_15m,
    passes_15m_iql,
    passes_15m_iql_relaxed_rule,
    sol_ltf_strength_15m,
)
from .adjustments import (
    rsi_4_level_adj_5m,
    rsi_4_level_adj_15m,
    sabre_tension_adj,
)
from .alt_gates import (
    AltHistGateResult,
    alt_1h_hist_gate,
    apply_primary_htf_bias,
    btc_catalyst_boost,
    sol_rsi_extremes_adj,
)
from .eth_follow import (
    FollowScoreResult,
    btc_follow_5m_impulse,
    btc_follow_15m_impulse_ok,
    eth_5m_macd_score,
    eth_15m_follow_score,
)
from .htf_boost import (
    HistGateResult,
    btc_5m_4h_1h_hist_gate,
    btc_5m_htf_boost,
    btc_15m_htf_boost,
)
from .m5_momentum import SolM5MacdResult, score_m5_direction, sol_m5_macd_adj
from .timing import TimingBonusResult, btc_15m_timing_bonus

__all__ = [
    "BtcHtfBiasResult",
    "btc_htf_bias",
    "LtfStrengthResult",
    "btc_ltf_strength_15m",
    "sol_ltf_strength_15m",
    "passes_15m_iql",
    "passes_15m_iql_relaxed_rule",
    "score_m5_direction",
    "SolM5MacdResult",
    "sol_m5_macd_adj",
    "HistGateResult",
    "btc_5m_htf_boost",
    "btc_5m_4h_1h_hist_gate",
    "btc_15m_htf_boost",
    "TimingBonusResult",
    "btc_15m_timing_bonus",
    "rsi_4_level_adj_5m",
    "rsi_4_level_adj_15m",
    "sabre_tension_adj",
    "AltHistGateResult",
    "alt_1h_hist_gate",
    "apply_primary_htf_bias",
    "btc_catalyst_boost",
    "sol_rsi_extremes_adj",
    "FollowScoreResult",
    "btc_follow_5m_impulse",
    "btc_follow_15m_impulse_ok",
    "eth_5m_macd_score",
    "eth_15m_follow_score",
]
