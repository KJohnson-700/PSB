"""Shared decision-core primitives used by both live strategies and the backtest engine.

Pure functions only. No IO, no logging, no config-object dependencies — callers
pass in scalars. The contract: live and backtest call the SAME function with the
SAME inputs and get the SAME output. Any drift between live and backtest must
live in the callers, not here.
"""

from .htf_bias import BtcHtfBiasResult, btc_htf_bias
from .ltf_strength import LtfStrengthResult, btc_ltf_strength_15m, sol_ltf_strength_15m

__all__ = [
    "BtcHtfBiasResult",
    "btc_htf_bias",
    "LtfStrengthResult",
    "btc_ltf_strength_15m",
    "sol_ltf_strength_15m",
]
