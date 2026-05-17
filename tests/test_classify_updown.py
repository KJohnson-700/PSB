"""Tests for dashboard updown trade bucket classification (ETH/XRP parity)."""

from src.dashboard.server import _classify_updown_trade


def test_eth_macro_fallback_buckets_15m():
    assert _classify_updown_trade("", "eth_macro", "") == "ETH_updown_15m"
    assert _classify_updown_trade("short", "eth_macro", "abc") == "ETH_updown_15m"


def test_eth_macro_fallback_buckets_5m_from_text():
    q = "Something5m window"
    assert _classify_updown_trade(q, "eth_macro", "") == "ETH_updown_5m"


def test_eth_macro_fallback_buckets_30m_from_text():
    q = "Something updown-30m hint"
    assert _classify_updown_trade(q, "eth_macro", "") == "ETH_updown_30m"


def test_eth_macro_fallback_buckets_1h_from_text():
    q = "ethereum-up-or-down-may-17-2026-1am-et"
    assert _classify_updown_trade(q, "eth_macro", "") == "ETH_updown_1h"


def test_xrp_macro_fallback_buckets():
    assert _classify_updown_trade("", "xrp_macro", "") == "XRP_updown_15m"
    assert _classify_updown_trade("xrp 5m", "xrp_macro", "") == "XRP_updown_5m"


def test_slug_hints():
    assert (
        _classify_updown_trade("", "unknown", "mkt-eth-updown-15m-123")
        == "ETH_updown_15m"
    )
    assert (
        _classify_updown_trade("", "unknown", "xrp-updown-5m-foo")
        == "XRP_updown_5m"
    )
    assert (
        _classify_updown_trade("", "unknown", "mkt-eth-updown-30m-xyz")
        == "ETH_updown_30m"
    )


def test_classic_question_parsing():
    q = "Ethereum Up or Down 2:15AM–2:30AM ET"
    assert _classify_updown_trade(q, "eth_macro", "") == "ETH_updown_15m"


def test_classic_question_parsing_30m_window():
    q = "Ethereum Up or Down - April 21, 2:15AM-2:45AM ET"
    assert _classify_updown_trade(q, "eth_macro", "") == "ETH_updown_30m"


def test_classic_question_parsing_1h_window():
    q = "Ethereum Up or Down - May 17, 1AM ET"
    assert _classify_updown_trade(q, "eth_macro", "") == "ETH_updown_1h"
