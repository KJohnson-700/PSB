from datetime import datetime, timedelta, timezone

from src.backtest.backtest_ai import BacktestAIAgent
from src.analysis.hyperliquid_hype_service import HyperliquidHypeService
from src.analysis.math_utils import PositionSizer
from src.market.scanner import (
    Market,
    MarketScanner,
    _WEATHER_TEMP_SLUG_RE,
    is_crypto_updown_market,
)
from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.sol_macro import SolMacroStrategy


def _market(question: str, slug: str = "") -> Market:
    return Market(
        id="m1",
        question=question,
        description="",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.5,
        no_price=0.5,
        spread=0.02,
        end_date=datetime.now() + timedelta(hours=1),
        token_id_yes="y",
        token_id_no="n",
        group_item_title="",
        slug=slug,
    )


def _config() -> dict:
    return {
        "strategies": {
            "sol_macro": {
                "enabled": True,
                "entry_window_auto_align": True,
                "entry_window_align_scan_interval_sec": 300,
                "entry_window_auto_align_max_expand_min": 1.0,
                "entry_window_auto_align_jitter_sec": 10,
            },
            "hype_macro": {
                "enabled": True,
                "entry_window_auto_align": True,
                "entry_window_align_scan_interval_sec": 300,
                "entry_window_auto_align_max_expand_min": 1.0,
                "entry_window_auto_align_jitter_sec": 10,
            },
        }
    }


def test_is_crypto_updown_market_detects_hype_slug_families():
    m1 = _market("HYPE Up or Down - April 20, 2:15AM-2:20AM ET", slug="hype-updown-5m-1776048900")
    m2 = _market("Hyperliquid Up or Down - April 20, 4:15AM-4:30AM ET", slug="hyperliquid-up-or-down-april-20-415am-430am-et")
    assert is_crypto_updown_market(m1) is True
    assert is_crypto_updown_market(m2) is True


def test_build_human_updown_event_slug_uses_named_btc_format():
    when_utc = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    slug = MarketScanner._build_human_updown_event_slug("bitcoin", when_utc)
    assert slug == "bitcoin-up-or-down-april-27-2026-3pm-et"


def test_build_human_updown_event_slug_handles_single_digit_day_and_am():
    when_utc = datetime(2026, 4, 7, 13, 0, tzinfo=timezone.utc)
    slug = MarketScanner._build_human_updown_event_slug("bitcoin", when_utc)
    assert slug == "bitcoin-up-or-down-april-7-2026-9am-et"


def test_weather_temperature_slug_regex_accepts_expected_slug_family():
    m = _WEATHER_TEMP_SLUG_RE.match("highest-temperature-in-manila-on-apr-29-2026")
    assert m is not None
    assert m.group(1) == "manila"


def test_weather_temperature_slug_regex_rejects_non_temperature_slug():
    assert _WEATHER_TEMP_SLUG_RE.match("will-it-rain-in-manila-on-apr-29-2026") is None


def test_hyperliquid_hype_service_parses_candle_snapshot(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "t": 1710000000000,
                    "T": 1710000299999,
                    "o": "1.10",
                    "h": "1.20",
                    "l": "1.05",
                    "c": "1.15",
                    "v": "1234.56",
                },
                {
                    "t": 1710000300000,
                    "T": 1710000599999,
                    "o": "1.15",
                    "h": "1.25",
                    "l": "1.10",
                    "c": "1.22",
                    "v": "1500.00",
                },
            ]

    def _fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("src.analysis.hyperliquid_hype_service.requests.post", _fake_post)
    svc = HyperliquidHypeService()
    df = svc.fetch_klines("HYPEUSDT", interval="5m", limit=2)
    assert len(df) == 2
    assert float(df["close"].iloc[-1]) == 1.22
    assert "open_time" in df.columns
    assert "close_time" in df.columns


def test_hype_strategy_keeps_entry_window_parity_with_sol():
    cfg = _config()
    ai = BacktestAIAgent(cfg)
    sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
    sol = SolMacroStrategy(cfg, ai, sizer)
    hype = HYPEMacroStrategy(cfg, ai, sizer)

    sol_bounds = sol._resolve_entry_window_bounds(is_5m=False, default_min=13.0, default_max=14.33)
    hype_bounds = hype._resolve_entry_window_bounds(is_5m=False, default_min=13.0, default_max=14.33)
    assert sol_bounds == hype_bounds
