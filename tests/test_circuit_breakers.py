from dataclasses import dataclass

from src.analysis.circuit_breakers import CircuitBreakerManager


@dataclass
class FakePosition:
    outcome: str
    entry_leg: str = ""
    strategy: str = "bitcoin"


def _cfg():
    return {
        "correlation_stop_halt": {
            "enabled": True,
            "stops_threshold": 3,
            "window_sec": 60,
            "slow_mode_enabled": True,
            "slow_stops_threshold": 6,
            "slow_window_sec": 900,
            "pause_minutes": 15,
            "same_side_only": True,
        },
        "reversal_halt": {
            "enabled": True,
            "btc_pct_threshold": 0.003,
            "position_threshold": 5,
            "lookback_sec": 300,
            "pause_minutes": 10,
        },
    }


def test_fast_correlation_stop_halts_same_side_entries():
    mgr = CircuitBreakerManager(_cfg())
    mgr.record_exit(reason="updown_stop_loss", action="BUY_NO", now=1000)
    mgr.record_exit(reason="updown_stop_loss", action="BUY_NO", now=1020)
    triggered = mgr.record_exit(reason="updown_stop_loss", action="BUY_NO", now=1040)

    assert triggered is not None
    assert triggered.allowed is False
    assert triggered.side == "BUY_NO"
    assert "correlation_stop_halt" in triggered.reason

    blocked = mgr.can_enter(action="BUY_NO", active_positions=[], now=1041)
    assert blocked.allowed is False

    allowed_other_side = mgr.can_enter(action="BUY_YES", active_positions=[], now=1041)
    assert allowed_other_side.allowed is True


def test_slow_correlation_stop_catches_bleed_without_fast_cluster():
    mgr = CircuitBreakerManager(_cfg())
    for i in range(5):
        mgr.record_exit(reason="updown_stop_loss", action="BUY_NO", now=1000 + i * 120)

    not_yet = mgr.can_enter(action="BUY_NO", active_positions=[], now=1490)
    assert not_yet.allowed is True

    triggered = mgr.record_exit(reason="updown_stop_loss", action="BUY_NO", now=1600)
    assert triggered is not None
    assert triggered.allowed is False
    assert "correlation_stop_slow_halt" in triggered.reason


def test_lane_stop_halt_blocks_only_configured_lane():
    cfg = _cfg()
    cfg["lane_stop_halt"] = {
        "enabled": True,
        "pause_minutes": 10,
        "lanes": [{"lane": "hype_macro|5m|BUY_YES"}],
    }
    mgr = CircuitBreakerManager(cfg)

    triggered = mgr.record_exit(
        reason="updown_stop_loss",
        action="BUY_YES",
        strategy="hype_macro",
        window="5m",
        now=1000,
    )

    assert triggered is not None
    assert triggered.allowed is False
    assert triggered.side == "BUY_YES"
    assert "lane_stop_halt" in triggered.reason

    blocked = mgr.can_enter(
        action="BUY_YES",
        strategy="hype_macro",
        window="5m",
        active_positions=[],
        now=1001,
    )
    assert blocked.allowed is False

    other_window = mgr.can_enter(
        action="BUY_YES",
        strategy="hype_macro",
        window="15m",
        active_positions=[],
        now=1001,
    )
    assert other_window.allowed is True

    other_strategy = mgr.can_enter(
        action="BUY_YES",
        strategy="xrp_macro",
        window="5m",
        active_positions=[],
        now=1001,
    )
    assert other_strategy.allowed is True


def test_lane_stop_halt_expires():
    cfg = _cfg()
    cfg["lane_stop_halt"] = {
        "enabled": True,
        "pause_minutes": 10,
        "lanes": [{"lane": "hype_macro|5m|BUY_YES", "pause_minutes": 2}],
    }
    mgr = CircuitBreakerManager(cfg)
    mgr.record_exit(
        reason="updown_stop_loss",
        action="BUY_YES",
        strategy="hype_macro",
        window="5m",
        now=1000,
    )

    blocked = mgr.can_enter(
        action="BUY_YES",
        strategy="hype_macro",
        window="5m",
        active_positions=[],
        now=1119,
    )
    assert blocked.allowed is False

    allowed = mgr.can_enter(
        action="BUY_YES",
        strategy="hype_macro",
        window="5m",
        active_positions=[],
        now=1121,
    )
    assert allowed.allowed is True


def test_reversal_halt_blocks_dominant_buy_no_after_btc_rally():
    mgr = CircuitBreakerManager(_cfg())
    positions = [FakePosition("NO") for _ in range(5)]
    mgr.record_btc_price(100_000, now=1000)

    decision = mgr.can_enter(
        action="BUY_NO",
        active_positions=positions,
        btc_price=100_350,
        now=1300,
    )

    assert decision.allowed is False
    assert decision.side == "BUY_NO"
    assert "reversal_halt" in decision.reason


def test_reversal_halt_does_not_block_offsetting_side():
    mgr = CircuitBreakerManager(_cfg())
    positions = [FakePosition("NO") for _ in range(5)]
    mgr.record_btc_price(100_000, now=1000)

    decision = mgr.can_enter(
        action="BUY_YES",
        active_positions=positions,
        btc_price=100_350,
        now=1300,
    )

    assert decision.allowed is True


def test_reversal_halt_requires_full_lookback_history():
    mgr = CircuitBreakerManager(_cfg())
    positions = [FakePosition("NO") for _ in range(5)]
    mgr.record_btc_price(100_000, now=1000)

    decision = mgr.can_enter(
        action="BUY_NO",
        active_positions=positions,
        btc_price=100_350,
        now=1100,
    )

    assert decision.allowed is True


def test_reversal_halt_ignores_non_crypto_positions():
    # A stale/unknown or non-crypto strategy label must not feed the crypto
    # reversal-halt breaker — otherwise those positions could halt crypto entries.
    mgr = CircuitBreakerManager(_cfg())
    positions = [FakePosition("NO", strategy="legacy_label") for _ in range(5)]
    mgr.record_btc_price(100_000, now=1000)

    decision = mgr.can_enter(
        action="BUY_NO",
        active_positions=positions,
        btc_price=100_350,
        now=1300,
    )

    assert decision.allowed is True


def test_reversal_halt_counts_crypto_updown_positions():
    # Crypto up/down labels DO feed the breaker: 5 same-side BTC positions trip it.
    mgr = CircuitBreakerManager(_cfg())
    positions = [FakePosition("NO", strategy="bitcoin") for _ in range(5)]
    mgr.record_btc_price(100_000, now=1000)

    decision = mgr.can_enter(
        action="BUY_NO",
        active_positions=positions,
        btc_price=100_350,
        now=1300,
    )

    assert decision.allowed is False
    assert decision.side == "BUY_NO"
    assert "reversal_halt" in decision.reason
