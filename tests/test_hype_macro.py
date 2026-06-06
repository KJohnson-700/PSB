from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.sol_macro import BiasResolution, SolMacroSignal

from tests.async_helpers import run_async


def _make_config():
    return {
        "strategies": {
            "hype_macro": {
                "enabled": True,
                "hard_min_edge": 0.0,
                "hourly_buy_yes_native_bonus_1h": 0.03,
                "hourly_buy_yes_native_bonus_min_ltf_strength_1h": 0.30,
                "hype_15m_neutral_fallback_buy_no_max_yes_price_bull_1h": 0.45,
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
    window_size: str,
    side_source: str,
    btc_1h_regime: str,
    action: str = "BUY_NO",
    yes_price: float = 0.50,
    edge: float = 0.08,
    alt_htf_bias: str = "NEUTRAL",
    convergence_score: float = 0.45,
) -> SolMacroSignal:
    price = yes_price if action == "BUY_YES" else (1.0 - yes_price)
    return SolMacroSignal(
        market_id=market_id,
        market_question=f"HYPE {window_size} market",
        action=action,
        price=price,
        size=10.0,
        confidence=0.7,
        edge=edge,
        token_id_yes=f"{market_id}-yes",
        token_id_no=f"{market_id}-no",
        end_date=datetime(2026, 5, 28, tzinfo=timezone.utc),
        direction="DOWN" if action == "BUY_NO" else "UP",
        ai_used=False,
        strategy_name="hype_macro",
        alt_asset_code="hype",
        side_source=side_source,
        btc_1h_regime=btc_1h_regime,
        alt_htf_bias=alt_htf_bias,
        convergence_score=convergence_score,
        window_size=window_size,
        reason="test",
    )


def test_hype_local_guard_blocks_5m_neutral_fallback_short():
    strategy = HYPEMacroStrategy(_make_config(), MagicMock(), MagicMock())

    reason = strategy._hype_signal_guard_reason(
        _signal(
            market_id="hype5m",
            window_size="5m",
            side_source="hype_5m_neutral_fallback_1h",
            btc_1h_regime="BULL",
        )
    )

    assert reason == "hype_5m_neutral_fallback_short_disabled"


def test_hype_scan_filters_local_guarded_signals():
    strategy = HYPEMacroStrategy(_make_config(), MagicMock(), MagicMock())
    guarded = _signal(
        market_id="hype_guarded",
        window_size="5m",
        side_source="hype_5m_neutral_fallback_1h",
        btc_1h_regime="BULL",
    )
    kept = _signal(
        market_id="hype_kept",
        window_size="5m",
        side_source="hype_5m_native",
        btc_1h_regime="BULL",
        yes_price=0.47,
    )

    with patch("src.strategies.sol_macro.SolMacroStrategy.scan_and_analyze", return_value=[guarded, kept]):
        signals = run_async(strategy.scan_and_analyze([], bankroll=10000.0))

    assert [signal.market_id for signal in signals] == ["hype_kept"]
    assert strategy.last_scan_stats["top_skip_reasons"]["local_hype_guard"] == 1


def test_hype_local_guard_blocks_native_buy_yes_when_neutral_1h_convergence_weak():
    strategy = HYPEMacroStrategy(_make_config(), MagicMock(), MagicMock())

    reason = strategy._hype_signal_guard_reason(
        _signal(
            market_id="hype_weak_native",
            window_size="15m",
            side_source="hype_15m_native",
            btc_1h_regime="RANGE",
            action="BUY_YES",
            alt_htf_bias="NEUTRAL",
            convergence_score=0.45,
        )
    )

    assert reason == "hype_native_buy_yes_alt_1h_neutral_weak_convergence"


def test_hype_local_guard_allows_native_buy_yes_when_neutral_1h_convergence_strong():
    strategy = HYPEMacroStrategy(_make_config(), MagicMock(), MagicMock())

    reason = strategy._hype_signal_guard_reason(
        _signal(
            market_id="hype_strong_native",
            window_size="15m",
            side_source="hype_15m_native",
            btc_1h_regime="RANGE",
            action="BUY_YES",
            alt_htf_bias="NEUTRAL",
            convergence_score=0.58,
        )
    )

    assert reason is None


def test_hype_hourly_buy_yes_native_bonus_is_opted_in():
    strategy = HYPEMacroStrategy(_make_config(), MagicMock(), MagicMock())
    native = BiasResolution(
        allowed_side="LONG",
        side_source="hype_1h_native",
        horizon_tf="1h",
        horizon_bias="BULLISH",
        slower_biases={},
        primary_htf_bias="BULLISH",
    )

    assert strategy._hourly_buy_yes_native_bonus(
        window_size="1h",
        allowed_side="LONG",
        resolution=native,
        ltf_strength=0.35,
    ) == 0.03
