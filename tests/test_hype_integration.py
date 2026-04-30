import asyncio
from datetime import datetime, timedelta, timezone

from src.backtest.backtest_ai import BacktestAIAgent
from src.analysis.hyperliquid_hype_service import HyperliquidHypeService
from src.analysis.math_utils import PositionSizer
from src.market.scanner import (
    Market,
    MarketScanner,
    _WEATHER_TEMP_SLUG_RE,
    _parse_updown_market_end_from_text,
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
        "polymarket": {},
        "trading": {"cycle_interval_sec": 120},
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
                "dynamic_beta_min": 0.50,
                "dynamic_beta_max": 4.00,
                "dynamic_beta_extreme_max": 6.00,
                "low_corr_threshold_1h": 0.40,
                "low_corr_damping": 0.75,
                "low_corr_suppresses_entries": True,
            },
            "xrp_macro": {
                "enabled": True,
                "dynamic_beta_min": 0.60,
                "dynamic_beta_max": 2.50,
                "dynamic_beta_extreme_max": 3.50,
                "low_corr_threshold_1h": 0.35,
                "low_corr_damping": 0.80,
            },
        }
    }


def test_is_crypto_updown_market_detects_hype_slug_families():
    m1 = _market("HYPE Up or Down - April 20, 2:15AM-2:20AM ET", slug="hype-updown-5m-1776048900")
    m2 = _market("Hyperliquid Up or Down - April 20, 4:15AM-4:30AM ET", slug="hyperliquid-up-or-down-april-20-415am-430am-et")
    assert is_crypto_updown_market(m1) is True
    assert is_crypto_updown_market(m2) is True


def test_hype_strategy_rejects_generic_hype_noun_market():
    cfg = _config()
    hype = HYPEMacroStrategy(cfg, BacktestAIAgent(cfg), PositionSizer())
    market = _market(
        "Will election hype peak before Friday?",
        slug="will-election-hype-peak-before-friday",
    )
    assert hype._is_solana_market(market) is False


def test_hype_and_xrp_default_to_updown_entry_band():
    from src.strategies.xrp_macro import XRPMacroStrategy

    cfg = _config()
    ai = BacktestAIAgent(cfg)
    sizer = PositionSizer()

    hype = HYPEMacroStrategy(cfg, ai, sizer)
    xrp = XRPMacroStrategy(cfg, ai, sizer)

    assert (hype.entry_price_min, hype.entry_price_max) == (0.46, 0.54)
    assert (xrp.entry_price_min, xrp.entry_price_max) == (0.46, 0.54)


def test_hype_and_xrp_apply_asset_specific_beta_and_corr_settings():
    from src.strategies.xrp_macro import XRPMacroStrategy

    cfg = _config()
    ai = BacktestAIAgent(cfg)
    sizer = PositionSizer()

    hype = HYPEMacroStrategy(cfg, ai, sizer)
    xrp = XRPMacroStrategy(cfg, ai, sizer)

    assert hype.sol_service.dynamic_beta_min == 0.50
    assert hype.sol_service.dynamic_beta_max == 4.00
    assert hype.sol_service.dynamic_beta_extreme_max == 6.00
    assert hype.low_corr_threshold_1h == 0.40
    assert hype.low_corr_damping == 0.75

    assert xrp.sol_service.dynamic_beta_min == 0.60
    assert xrp.sol_service.dynamic_beta_max == 2.50
    assert xrp.sol_service.dynamic_beta_extreme_max == 3.50
    assert xrp.low_corr_threshold_1h == 0.35
    assert xrp.low_corr_damping == 0.80


def test_updown_slug_iterator_uses_timestamp_market_slugs():
    slugs = MarketScanner._iter_updown_event_slugs(step_minutes=15, look_ahead=0)
    assert any(slug.startswith("btc-updown-15m-") for slug in slugs)
    assert any(slug.startswith("sol-updown-15m-") for slug in slugs)
    assert any(slug.startswith("eth-updown-15m-") for slug in slugs)
    assert any(slug.startswith("xrp-updown-15m-") for slug in slugs)
    assert any(slug.startswith("hype-updown-15m-") for slug in slugs)
    assert not any("up-or-down" in slug for slug in slugs)


def test_build_human_updown_event_slug_uses_named_btc_format():
    when_utc = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    slug = MarketScanner._build_human_updown_event_slug("bitcoin", when_utc)
    assert slug == "bitcoin-up-or-down-april-27-2026-3pm-et"


def test_build_human_updown_event_slug_handles_single_digit_day_and_am():
    when_utc = datetime(2026, 4, 7, 13, 0, tzinfo=timezone.utc)
    slug = MarketScanner._build_human_updown_event_slug("bitcoin", when_utc)
    assert slug == "bitcoin-up-or-down-april-7-2026-9am-et"


def test_parse_updown_market_end_uses_individual_candle_range():
    end = _parse_updown_market_end_from_text(
        slug="bitcoin-up-or-down-april-28-2026-10pm-et",
        question="Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
        group_item_title="",
    )
    assert end == datetime(2026, 4, 29, 2, 15, tzinfo=timezone.utc)


def test_parse_updown_market_end_uses_group_item_title_when_question_is_generic():
    end = _parse_updown_market_end_from_text(
        slug="hyperliquid-up-or-down-april-28-2026-10pm-et",
        question="Hyperliquid Up or Down",
        group_item_title="April 28, 10:15PM-10:20PM ET",
    )
    assert end == datetime(2026, 4, 29, 2, 20, tzinfo=timezone.utc)


def test_parse_updown_market_end_handles_midnight_rollover():
    end = _parse_updown_market_end_from_text(
        slug="bitcoin-up-or-down-april-28-2026-11pm-et",
        question="Bitcoin Up or Down - April 28, 11:55PM-12:00AM ET",
        group_item_title="",
    )
    assert end == datetime(2026, 4, 29, 4, 0, tzinfo=timezone.utc)


def test_parse_gamma_event_market_prefers_candle_end_over_event_end():
    market = MarketScanner._parse_gamma_event_market(
        {
            "id": "m1",
            "question": "Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
            "groupItemTitle": "",
            "outcomePrices": "[\"0.51\", \"0.49\"]",
            "clobTokenIds": "[\"yes\", \"no\"]",
            "volume": "1000",
            "liquidity": "1000",
            "endDate": "2026-04-29T03:00:00Z",
        },
        "bitcoin-up-or-down-april-28-2026-10pm-et",
    )
    assert market is not None
    assert market.end_date == datetime(2026, 4, 29, 2, 15, tzinfo=timezone.utc)


def test_weather_temperature_slug_regex_accepts_expected_slug_family():
    m = _WEATHER_TEMP_SLUG_RE.match("highest-temperature-in-manila-on-apr-29-2026")
    assert m is not None
    assert m.group(1) == "manila"


def test_weather_temperature_slug_regex_rejects_non_temperature_slug():
    assert _WEATHER_TEMP_SLUG_RE.match("will-it-rain-in-manila-on-apr-29-2026") is None


def test_dedicated_weather_candidate_accepts_weather_slug_and_title():
    market = _market(
        "Will Hong Kong have between 130-140mm of precipitation in April?",
        slug="will-hong-kong-have-between-130-140mm-of-precipitation-in-april",
    )
    assert MarketScanner._is_dedicated_weather_candidate(market) is True


def test_dedicated_weather_candidate_rejects_generic_non_weather_market():
    market = _market(
        "Will candidate X win in April?",
        slug="will-candidate-x-win-in-april",
    )
    market = Market(
        **{
            **market.__dict__,
            "description": "General politics market with no weather context.",
            "group_item_title": "Politics",
        }
    )
    assert MarketScanner._is_dedicated_weather_candidate(market) is False


def test_dedicated_weather_candidate_rejects_false_positive_wind_name_slug():
    market = _market(
        "Will Jonas Wind be the top goal scorer in the 2025-26 Bundesliga season?",
        slug="will-jonas-wind-be-the-top-goal-scorer-in-the-2025-26-bundesliga-season",
    )
    assert MarketScanner._is_dedicated_weather_candidate(market) is False


def test_dedicated_weather_candidate_rejects_false_positive_hail_title():
    market = _market(
        "Will Project Hail Mary be the top grossing movie of 2026?",
        slug="will-project-hail-mary-be-the-top-grossing-movie-of-2026",
    )
    assert MarketScanner._is_dedicated_weather_candidate(market) is False


def test_dedicated_weather_candidate_rejects_named_storm_market_without_precip_structure():
    market = _market(
        "Will a named storm form before hurricane season?",
        slug="will-a-named-storm-form-before-hurricane-season",
    )
    assert MarketScanner._is_dedicated_weather_candidate(market) is False


def test_scanner_timeout_is_capped_below_cycle_interval():
    cfg = _config()
    cfg["polymarket"]["scanner_sync_timeout_sec"] = 180
    cfg["trading"]["cycle_interval_sec"] = 120
    scanner = MarketScanner(cfg)
    assert scanner._scanner_sync_timeout == 105.0


def test_hype_low_correlation_hard_gate_is_configured():
    cfg = _config()
    strategy = HYPEMacroStrategy(cfg, BacktestAIAgent(cfg), PositionSizer())

    assert strategy.low_corr_suppresses_entries is True
    assert strategy.low_corr_threshold_1h == 0.40


def test_scanner_sync_phase_returns_core_markets_when_optional_fetch_times_out(monkeypatch):
    import time

    cfg = _config()
    cfg["polymarket"]["scanner_sync_timeout_sec"] = 0.05
    cfg["polymarket"]["fetch_hype_alt_markets"] = True
    cfg["strategies"]["weather"] = {"enabled": True, "scan_limit": 20}
    scanner = MarketScanner(cfg)

    core_15m = [_market("Bitcoin Up or Down - test", slug="btc-updown-15m-test")]
    core_5m = [_market("Solana Up or Down - test", slug="sol-updown-5m-test")]

    monkeypatch.setattr(scanner, "_fetch_markets_gamma", lambda limit=200: [])
    monkeypatch.setattr(scanner, "fetch_updown_markets", lambda look_ahead=8: core_15m)
    monkeypatch.setattr(scanner, "fetch_updown_5m_markets", lambda look_ahead=8: core_5m)
    monkeypatch.setattr(scanner, "fetch_weather_markets", lambda limit=20: [])

    def _slow_hype(limit=100):
        time.sleep(0.2)
        return [_market("Hyperliquid Up or Down - late", slug="hyperliquid-up-or-down-test")]

    monkeypatch.setattr(scanner, "fetch_hype_alt_updown_markets", _slow_hype)

    markets, updown, updown_5m, hype_alt, weather, look_15m, look_5m = scanner._sync_network_phase()

    assert markets == []
    assert updown == core_15m
    assert updown_5m == core_5m
    assert hype_alt == []
    assert weather == []
    assert look_15m >= 1
    assert look_5m >= 1


def test_scan_for_opportunities_high_liquidity_includes_updown_snapshot(monkeypatch):
    cfg = _config()
    cfg["polymarket"]["fetch_hype_alt_markets"] = False
    cfg["polymarket"]["fetch_weather_markets"] = False
    scanner = MarketScanner(cfg)

    btc_15m = _market("Bitcoin Up or Down - April 30, 1:00PM ET", slug="bitcoin-up-or-down-april-30-2026-1pm-et")
    btc_15m.id = "snapshot-btc-15m"
    btc_15m.window_minutes = 15
    sol_5m = _market("Solana Up or Down - April 30, 1:00PM ET", slug="solana-up-or-down-april-30-2026-1pm-et")
    sol_5m.id = "snapshot-sol-5m"
    sol_5m.window_minutes = 5

    monkeypatch.setattr(
        scanner,
        "_sync_network_phase",
        lambda: ([], [btc_15m], [sol_5m], [], [], 8, 8),
    )

    async def _identity(markets):
        return markets

    monkeypatch.setattr(scanner, "update_market_prices", _identity)

    try:
        opportunities = asyncio.run(scanner.scan_for_opportunities())
    finally:
        asyncio.run(scanner.close())

    high_liquidity_ids = {m.id for m in opportunities["high_liquidity"]}
    assert {"snapshot-btc-15m", "snapshot-sol-5m"} <= high_liquidity_ids
    assert opportunities["scanner_meta"]["updown_15m_count"] == 1
    assert opportunities["scanner_meta"]["updown_5m_count"] == 1


def test_scanner_weather_fetch_uses_background_cache_after_slow_refresh(monkeypatch):
    import time

    cfg = _config()
    cfg["polymarket"]["scanner_sync_timeout_sec"] = 0.05
    cfg["strategies"]["weather"] = {"enabled": True, "scan_limit": 20}
    scanner = MarketScanner(cfg)

    weather_market = _market(
        "Will NYC have less than 2 inches of precipitation in April?",
        slug="will-nyc-have-less-than-2-inches-of-precipitation-in-april",
    )

    monkeypatch.setattr(scanner, "_fetch_markets_gamma", lambda limit=200: [])
    monkeypatch.setattr(scanner, "fetch_updown_markets", lambda look_ahead=8: [])
    monkeypatch.setattr(scanner, "fetch_updown_5m_markets", lambda look_ahead=8: [])

    def _slow_weather(limit=20):
        time.sleep(0.08)
        return [weather_market]

    monkeypatch.setattr(scanner, "fetch_weather_markets", _slow_weather)

    first = scanner._sync_network_phase()
    assert first[4] == []

    time.sleep(0.12)
    second = scanner._sync_network_phase()
    assert [m.id for m in second[4]] == [weather_market.id]


def test_weather_scan_limit_defaults_to_120():
    scanner = MarketScanner(_config())
    assert scanner.weather_scan_limit == 120


def test_optional_hype_and_weather_fetches_can_be_disabled():
    cfg = _config()
    cfg["polymarket"]["fetch_hype_alt_markets"] = False
    cfg["polymarket"]["fetch_weather_markets"] = False
    cfg["strategies"]["weather"] = {"enabled": True}
    scanner = MarketScanner(cfg)
    assert scanner._should_fetch_hype_alt_markets() is False
    assert scanner._should_fetch_weather_markets() is False


def test_hype_alt_fetch_uses_direct_slug_queries_not_event_pagination(monkeypatch):
    cfg = _config()
    scanner = MarketScanner(cfg)
    seen_params = []

    class _Resp:
        status_code = 200

        def json(self):
            return []

    def _fake_get(url, params=None, timeout=None):
        seen_params.append(dict(params or {}))
        return _Resp()

    monkeypatch.setattr("src.market.scanner.requests.get", _fake_get)
    scanner.fetch_hype_alt_updown_markets(limit=10)

    assert seen_params
    assert all("slug" in params for params in seen_params)
    assert all("offset" not in params for params in seen_params)


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
