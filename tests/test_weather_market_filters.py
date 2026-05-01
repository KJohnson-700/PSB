"""Regression: crypto up/down markets must not pass weather pipeline filters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.main import _filter_weather_markets, _is_crypto_market
from src.market.scanner import Market
from src.strategies.weather import WeatherStrategy


def _m(
    *,
    question: str,
    slug: str = "",
    window_minutes=None,
    hours_to_end: float = 12.0,
) -> Market:
    end = datetime.now(timezone.utc) + timedelta(hours=hours_to_end)
    return Market(
        id="m1",
        question=question,
        description="",
        volume=5000.0,
        liquidity=5000.0,
        yes_price=0.5,
        no_price=0.5,
        spread=0.02,
        end_date=end,
        token_id_yes="y",
        token_id_no="n",
        group_item_title="",
        slug=slug,
        window_minutes=window_minutes,
    )


def test_is_crypto_solana_slug_without_window_minutes():
    m = _m(
        question="Solana Up or Down - April 30, 2026, 10:00PM-10:15PM ET",
        slug="sol-updown-15m-1234567890",
        window_minutes=None,
        hours_to_end=0.5,
    )
    assert _is_crypto_market(m) is True


def test_is_crypto_not_monthly_weather_with_naive_end():
    """Old 24h fallback misclassified long-window / TZ-skewed markets as crypto."""
    m = _m(
        question="Will Seattle get more than 2.5 inches of rain in April 2026?",
        slug="seattle-april-rain",
        window_minutes=None,
        hours_to_end=6.0,
    )
    assert _is_crypto_market(m) is False


def test_filter_weather_markets_drops_crypto():
    weather_like = _m(
        question="Will it rain in Seattle on May 1?",
        slug="sea-rain-may1",
        hours_to_end=30.0,
    )
    sol = _m(
        question="Solana Up or Down - April 30, 2026, 10:00PM-10:15PM ET",
        slug="sol-updown-15m-999",
        hours_to_end=0.25,
    )
    cfg = {
        "strategies": {
            "weather": {
                "resolution_window_enabled": True,
                "min_hours_to_resolution": 2.0,
                "max_hours_to_resolution": 72.0,
            }
        }
    }
    out = _filter_weather_markets([weather_like, sol], cfg)
    assert len(out) == 1
    assert "Seattle" in out[0].question


def test_is_crypto_explicit_short_window_minutes():
    m = _m(question="Some market", slug="", window_minutes=15, hours_to_end=0.25)
    assert _is_crypto_market(m) is True

    m2 = _m(question="Some market", slug="", window_minutes=60, hours_to_end=2.0)
    assert _is_crypto_market(m2) is False


def test_extended_period_precip_detection():
    assert WeatherStrategy._is_extended_period_precip(
        "Will Seattle receive 2.5+ inches of rain in April 2026?",
        "",
    )
    assert not WeatherStrategy._is_extended_period_precip(
        "Will it rain in Seattle on May 15?",
        "",
    )
