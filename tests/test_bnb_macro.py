from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.strategies.bnb_macro import BNBMacroStrategy
from src.strategies.sol_macro import SolMacroSignal

from tests.async_helpers import run_async


def _make_config():
    return {
        "strategies": {
            "bnb_macro": {
                "enabled": True,
                "bnb_5m_native_buy_no_max_yes_price_bull_1h": 0.60,
            }
        },
        "exposure": {
            "full_size": 5.0,
            "moderate_size": 3.0,
            "minimal_size": 1.0,
            "max_consecutive_losses": 3,
            "pause_cycles": 2,
        },
        "trading": {"dry_run": True},
    }


def _signal(
    *,
    market_id: str,
    action: str = "BUY_NO",
    window_size: str = "5m",
    side_source: str = "bnb_5m_native",
    btc_1h_regime: str = "BULL",
    alt_htf_bias: str = "BEARISH",
    yes_price: float = 0.55,
) -> SolMacroSignal:
    return SolMacroSignal(
        market_id=market_id,
        market_question="BNB Up or Down - test",
        action=action,
        price=yes_price if action == "BUY_YES" else (1.0 - yes_price),
        size=10.0,
        confidence=0.6,
        edge=0.08,
        token_id_yes=f"{market_id}-yes",
        token_id_no=f"{market_id}-no",
        end_date=datetime(2026, 5, 28, tzinfo=timezone.utc),
        direction="DOWN" if action == "BUY_NO" else "UP",
        strategy_name="bnb_macro",
        alt_asset_code="bnb",
        side_source=side_source,
        btc_1h_regime=btc_1h_regime,
        alt_htf_bias=alt_htf_bias,
        window_size=window_size,
        reason="test",
    )


def test_bnb_local_guard_blocks_5m_neutral_fallback_short():
    strategy = BNBMacroStrategy(_make_config(), MagicMock(), MagicMock())

    reason = strategy._bnb_signal_guard_reason(
        _signal(
            market_id="bnb-guard-1",
            side_source="bnb_5m_neutral_fallback_15m",
            yes_price=0.58,
        )
    )

    assert reason == "bnb_5m_neutral_fallback_short_disabled"


def test_bnb_local_guard_blocks_5m_native_short_when_alt_h1_not_bearish():
    strategy = BNBMacroStrategy(_make_config(), MagicMock(), MagicMock())

    reason = strategy._bnb_signal_guard_reason(
        _signal(
            market_id="bnb-guard-2",
            side_source="bnb_5m_native",
            alt_htf_bias="NEUTRAL",
            yes_price=0.555,
        )
    )

    assert reason == "bnb_5m_native_short_requires_bearish_alt_1h"


def test_bnb_buy_yes_signal_is_not_blocked_by_buy_no_local_guard():
    strategy = BNBMacroStrategy(_make_config(), MagicMock(), MagicMock())

    reason = strategy._bnb_signal_guard_reason(
        _signal(
            market_id="bnb-buy-yes",
            action="BUY_YES",
            side_source="bnb_5m_native",
            alt_htf_bias="BULLISH",
            yes_price=0.54,
        )
    )

    assert reason is None


def test_bnb_scan_filters_local_guarded_signals_only():
    strategy = BNBMacroStrategy(_make_config(), MagicMock(), MagicMock())
    guarded = _signal(
        market_id="bnb-guarded",
        side_source="bnb_5m_neutral_fallback_1h",
        yes_price=0.66,
    )
    kept = _signal(
        market_id="bnb-kept",
        side_source="bnb_15m_native",
        window_size="15m",
        yes_price=0.54,
    )

    with patch("src.strategies.sol_macro.SolMacroStrategy.scan_and_analyze", return_value=[guarded, kept]):
        signals = run_async(strategy.scan_and_analyze([], bankroll=10000.0))

    assert [signal.market_id for signal in signals] == ["bnb-kept"]
    assert strategy.last_scan_stats["top_skip_reasons"]["local_bnb_guard"] == 1
