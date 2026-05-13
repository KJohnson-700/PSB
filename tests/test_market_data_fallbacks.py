from datetime import datetime, timedelta

import pandas as pd

from src.analysis.btc_price_service import BTCPriceService
from src.analysis.hyperliquid_hype_service import (
    HyperliquidHypeService,
    _HL_MAX_CANDLES_PER_RANGE_REQUEST,
)
from src.analysis.sol_btc_service import SOLBTCService


def _sample_klines_df(close_value: float) -> pd.DataFrame:
    now = pd.Timestamp.utcnow()
    return pd.DataFrame(
        {
            "open_time": [now - pd.Timedelta(minutes=15)],
            "open": [close_value - 10.0],
            "high": [close_value + 10.0],
            "low": [close_value - 20.0],
            "close": [close_value],
            "volume": [123.0],
            "close_time": [now],
        }
    )


def test_btc_fetch_klines_uses_stale_cache_when_binance_unreachable(monkeypatch):
    svc = BTCPriceService()
    cache_key = "binance_1h_200"
    expected = _sample_klines_df(100000.0)
    svc._cache[cache_key] = (pd.Timestamp.utcnow().timestamp() - 30, expected)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Failed to resolve api3.binance.com")

    monkeypatch.setattr(svc._http_session, "get", _raise)

    out = svc.fetch_klines("1h", 200)
    assert not out.empty
    assert float(out.iloc[-1]["close"]) == 100000.0


def test_btc_price_falls_back_to_chainlink(monkeypatch):
    svc = BTCPriceService()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Failed to resolve api.binance.com")

    monkeypatch.setattr(svc._http_session, "get", _raise)
    monkeypatch.setattr(
        svc,
        "get_chainlink_price",
        lambda: (98765.0, datetime.utcnow() - timedelta(seconds=20)),
    )

    price = svc.get_current_price()
    assert price == 98765.0


def test_btc_price_falls_back_to_coingecko(monkeypatch):
    svc = BTCPriceService()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Failed to resolve api.binance.com")

    monkeypatch.setattr(svc._http_session, "get", _raise)
    monkeypatch.setattr(svc, "get_chainlink_price", lambda: (None, None))
    monkeypatch.setattr(svc, "_get_coingecko_price", lambda _asset: 97531.0)

    price = svc.get_current_price()
    assert price == 97531.0


def test_sol_price_falls_back_to_cached_close(monkeypatch):
    svc = SOLBTCService()
    cache_key = "binance_SOLUSDT_1m_60"
    expected = _sample_klines_df(142.25)
    svc._cache[cache_key] = (pd.Timestamp.utcnow().timestamp() - 40, expected)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Failed to resolve api.binance.com")

    monkeypatch.setattr(svc._http_session, "get", _raise)
    monkeypatch.setattr(svc, "get_chainlink_price_for_symbol", lambda _sym: (None, None, None))

    price = svc.get_current_price("SOLUSDT")
    assert price == 142.25


def test_sol_price_falls_back_to_coingecko(monkeypatch):
    svc = SOLBTCService()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Failed to resolve api.binance.com")

    monkeypatch.setattr(svc._http_session, "get", _raise)
    monkeypatch.setattr(svc, "get_chainlink_price_for_symbol", lambda _sym: (None, None, None))
    monkeypatch.setattr(svc, "_get_coingecko_price", lambda _sym: 141.11)

    price = svc.get_current_price("SOLUSDT")
    assert price == 141.11


def test_chainlink_polygon_rpcs_can_be_overridden_from_env(monkeypatch):
    monkeypatch.setenv(
        "CHAINLINK_POLYGON_RPCS",
        "https://rpc.custom.one, https://rpc.custom.two, https://rpc.custom.one",
    )
    btc = BTCPriceService()
    sol = SOLBTCService()
    assert btc.polygon_rpcs[0] == "https://rpc.custom.one"
    assert btc.polygon_rpcs[1] == "https://rpc.custom.two"
    assert sol.polygon_rpcs[0] == "https://rpc.custom.one"
    assert sol.polygon_rpcs[1] == "https://rpc.custom.two"


def test_chainlink_arbitrum_rpcs_can_be_overridden_from_env(monkeypatch):
    monkeypatch.setenv(
        "CHAINLINK_ARBITRUM_RPCS",
        "https://arb.custom.one,https://arb.custom.two",
    )
    svc = SOLBTCService()
    arbs = svc._chainlink_rpcs_for_network("arbitrum")
    assert arbs[0] == "https://arb.custom.one"
    assert arbs[1] == "https://arb.custom.two"


def test_hype_range_failure_returns_schema_empty_frame(monkeypatch):
    svc = HyperliquidHypeService()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Failed to resolve api.hyperliquid.xyz")

    monkeypatch.setattr(svc, "_post_candles", _raise)
    out = svc.fetch_klines_range(interval="1m", start_date="2026-01-20", end_date="2026-01-21")
    assert list(out.columns) == ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    assert out.empty


def test_hype_range_request_uses_requested_dates(monkeypatch):
    svc = HyperliquidHypeService()
    payloads = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    def _fake_post(payload, *, timeout):
        payloads.append(payload)
        return _Resp()

    monkeypatch.setattr(svc, "_post_candles", _fake_post)
    svc.fetch_klines_range(interval="15m", start_date="2026-01-20", end_date="2026-04-20")

    assert payloads
    first = payloads[0]["req"]
    start_ms = int(pd.Timestamp("2026-01-20", tz="UTC").timestamp() * 1000)
    global_end_ms = int(
        (pd.Timestamp("2026-04-21", tz="UTC") - pd.Timedelta(milliseconds=1)).timestamp() * 1000
    )
    assert first["startTime"] == start_ms
    chunk_span_ms = _HL_MAX_CANDLES_PER_RANGE_REQUEST * 15 * 60 * 1000
    assert first["endTime"] == min(start_ms + chunk_span_ms - 1, global_end_ms)
