from types import SimpleNamespace

from src.main import (
    _detect_window_from_question,
    _filter_crypto_hourly_markets,
    _should_include_hourly_crypto_markets,
)


def test_detect_window_from_hourly_question():
    q = "Bitcoin Up or Down - May 17, 1AM ET"
    assert _detect_window_from_question(q) == "1h"


def test_detect_window_from_legacy_thirty_minute_range():
    q = "Bitcoin Up or Down - April 21, 1:30AM-2:00AM ET"
    assert _detect_window_from_question(q) == "30m"


def test_hourly_crypto_markets_throttled_every_third_cycle_by_default():
    cfg = {"trading": {}}
    assert _should_include_hourly_crypto_markets(cfg, 1) is True
    assert _should_include_hourly_crypto_markets(cfg, 2) is False
    assert _should_include_hourly_crypto_markets(cfg, 3) is False
    assert _should_include_hourly_crypto_markets(cfg, 4) is True


def test_filter_crypto_hourly_markets_keeps_5m_and_15m_when_hourly_skipped():
    m5 = SimpleNamespace(
        question="Bitcoin Up or Down - May 20, 6:10AM-6:15AM ET",
        group_item_title="",
        slug="btc-updown-5m-123",
        window_minutes=5,
    )
    m15 = SimpleNamespace(
        question="Bitcoin Up or Down - May 20, 6:15AM-6:30AM ET",
        group_item_title="",
        slug="btc-updown-15m-123",
        window_minutes=15,
    )
    m1h = SimpleNamespace(
        question="Bitcoin Up or Down - May 20, 7AM ET",
        group_item_title="",
        slug="bitcoin-up-or-down-may-20-2026-7am-et",
        window_minutes=60,
    )
    kept = _filter_crypto_hourly_markets([m5, m15, m1h], include_hourly=False)
    assert kept == [m5, m15]
