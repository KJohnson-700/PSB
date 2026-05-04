from datetime import datetime, timezone

from src.market.scanner import MarketScanner


def _config() -> dict:
    return {
        "polymarket": {},
        "trading": {"cycle_interval_sec": 120},
        "strategies": {
            "bitcoin": {"enabled": True},
            "sol_macro": {"enabled": True},
            "eth_macro": {"enabled": True},
            "xrp_macro": {"enabled": True},
            "hype_macro": {"enabled": True},
        },
    }


def test_parse_gamma_event_market_accepts_array_fields():
    market = MarketScanner._parse_gamma_event_market(
        {
            "id": "m1",
            "question": "Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
            "groupItemTitle": "",
            "outcomePrices": ["0.52", "0.48"],
            "clobTokenIds": ["yes", "no"],
            "volume": "1000",
            "liquidity": "1000",
            "endDate": "2026-04-29T03:00:00Z",
        },
        "bitcoin-up-or-down-april-28-2026-10pm-et",
    )
    assert market is not None
    assert market.token_id_yes == "yes"
    assert market.token_id_no == "no"
    assert market.end_date == datetime(2026, 4, 29, 2, 15, tzinfo=timezone.utc)


def test_fetch_event_slug_markets_records_per_source_slug_stats(monkeypatch):
    scanner = MarketScanner(_config())

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    def _fake_get(url, params=None, timeout=None):  # noqa: ARG001
        slug = (params or {}).get("slug", "")
        if "hit" in slug:
            return _Resp(
                [
                    {
                        "id": "hit-1",
                        "question": "Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
                        "groupItemTitle": "",
                        "outcomePrices": "[\"0.51\", \"0.49\"]",
                        "clobTokenIds": "[\"yes\", \"no\"]",
                        "volume": "1000",
                        "liquidity": "1000",
                    }
                ]
            )
        return _Resp([])

    monkeypatch.setattr("src.market.scanner.requests.get", _fake_get)

    markets = scanner._fetch_event_slug_markets(
        ["btc-updown-15m-hit", "btc-updown-15m-miss"],
        timeout_sec=1,
        stats_key="test_source",
    )
    assert len(markets) == 1

    stats = scanner._get_slug_fetch_stats_snapshot().get("test_source")
    assert stats is not None
    assert stats["attempted_slugs"] == 2
    assert stats["hit_slugs"] == 1
    assert stats["empty_slug_responses"] == 1


def test_empty_slug_cache_skips_repeat_lookup_within_cycle(monkeypatch):
    scanner = MarketScanner(_config())
    calls = {"count": 0}

    class _Resp:
        status_code = 200

        def json(self):
            return []

    def _fake_get(url, params=None, timeout=None):  # noqa: ARG001
        calls["count"] += 1
        return _Resp()

    monkeypatch.setattr("src.market.scanner.requests.get", _fake_get)

    scanner._fetch_event_slug_markets(["btc-updown-15m-empty"], timeout_sec=1)
    # Empty /markets then /events attempt before caching miss as empty.
    assert calls["count"] == 2
    scanner._fetch_event_slug_markets(["btc-updown-15m-empty"], timeout_sec=1)
    assert calls["count"] == 2


def test_updown_slug_falls_through_to_events_when_markets_empty(monkeypatch):
    scanner = MarketScanner(_config())
    gm_payload = {
        "id": "from-event",
        "question": "Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
        "groupItemTitle": "",
        "outcomePrices": "[\"0.51\", \"0.49\"]",
        "clobTokenIds": "[\"yes\", \"no\"]",
        "volume": "1000",
        "liquidity": "1000",
    }

    def _fake_get(url, params=None, timeout=None):  # noqa: ARG001
        if "/markets" in url:
            class _MarketsResp:
                status_code = 200

                def json(self_inner):
                    return []

            return _MarketsResp()
        if "/events" in url:
            class _EventsResp:
                status_code = 200

                def json(self_inner):
                    return [{"markets": [gm_payload]}]

            return _EventsResp()
        raise AssertionError(url)

    monkeypatch.setattr("src.market.scanner.requests.get", _fake_get)

    markets = scanner._fetch_event_slug_markets(
        ["btc-updown-15m-1234567890"], timeout_sec=1
    )
    assert len(markets) == 1
    assert markets[0].id == "from-event"
