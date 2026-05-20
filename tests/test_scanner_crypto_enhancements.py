from datetime import datetime, timezone

from src.market.scanner import Market, MarketScanner


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


def test_iter_updown_1h_human_slugs_shape():
    # May 13, 09:44 UTC = May 13, 05:44 EDT → hour floor 5AM ET
    ref = datetime(2026, 5, 13, 9, 44, tzinfo=timezone.utc)
    slugs = MarketScanner._iter_updown_1h_human_slugs(look_ahead=0, now_utc=ref)
    assert len(slugs) == 7
    assert "bitcoin-up-or-down-may-13-2026-5am-et" in slugs
    assert "ethereum-up-or-down-may-13-2026-5am-et" in slugs
    assert "solana-up-or-down-may-13-2026-5am-et" in slugs
    assert "xrp-up-or-down-may-13-2026-5am-et" in slugs
    # HYPE uses the short ``hype-`` prefix on Polymarket hourly (not ``hyperliquid-``).
    assert "hype-up-or-down-may-13-2026-5am-et" in slugs
    # DOGE is the inverse of the short-window slug family here: hourly uses
    # ``dogecoin-`` rather than ``doge-`` on live Gamma.
    assert "dogecoin-up-or-down-may-13-2026-5am-et" in slugs
    assert "bnb-up-or-down-may-13-2026-5am-et" in slugs
    assert not any("hyperliquid" in s for s in slugs)
    assert not any(slug.startswith("doge-up-or-down-") for slug in slugs)


def test_resolve_updown_lookahead_includes_doge_and_bnb_configs():
    scanner = MarketScanner(
        {
            "polymarket": {},
            "trading": {"cycle_interval_sec": 120},
            "strategies": {
                "doge_macro": {
                    "enabled": True,
                    "look_ahead_15m": 11,
                    "look_ahead_5m": 9,
                    "look_ahead_1h": 6,
                },
                "bnb_macro": {
                    "enabled": True,
                    "look_ahead_15m": 7,
                    "look_ahead_5m": 5,
                    "look_ahead_1h": 8,
                },
            },
        }
    )
    assert scanner._resolve_updown_lookahead() == (11, 9, 8)


def test_parse_gamma_event_market_accepts_array_fields():
    market = MarketScanner._parse_gamma_event_market(
        {
            "id": "m1",
            "question": "Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
            "groupItemTitle": "",
            "outcomePrices": ["0.52", "0.48"],
            "clobTokenIds": ["yes", "no"],
            "volume": "50000",
            "liquidity": "50000",
            "endDate": "2026-04-29T03:00:00Z",
        },
        "bitcoin-up-or-down-april-28-2026-10pm-et",
    )
    assert market is not None
    assert market.token_id_yes == "yes"
    assert market.token_id_no == "no"
    assert market.end_date == datetime(2026, 4, 29, 2, 15, tzinfo=timezone.utc)


def test_fetch_markets_gamma_closes_bulk_response(monkeypatch):
    scanner = MarketScanner(_config())
    closed = {"count": 0}
    payload = [
        {
            "id": "m1",
            "question": "Bitcoin Up or Down - April 28, 10:00PM-10:15PM ET",
            "description": "",
            "outcomePrices": ["0.52", "0.48"],
            "clobTokenIds": ["yes", "no"],
            "volume": "50000",
            "liquidity": "50000",
            "spread": "0.02",
            "endDate": "2026-04-29T02:15:00Z",
            "groupItemTitle": "",
            "slug": "btc-updown-15m-123",
        }
    ]

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def close(self):
            closed["count"] += 1

    def _fake_gamma_get(path, params=None, timeout=None):  # noqa: ARG001
        if (params or {}).get("offset", 0) == 0:
            return _Resp(payload)
        return _Resp([])

    monkeypatch.setattr(scanner, "_gamma_get", _fake_gamma_get)

    markets = scanner._fetch_markets_gamma(limit=1)
    assert len(markets) == 1
    assert closed["count"] == 1


def test_gamma_requests_session_reused_per_thread_and_closed_on_scanner_close(monkeypatch):
    scanner = MarketScanner(_config())
    created = []
    closed = []

    class _FakeSession:
        def __init__(self):
            created.append(self)

        def mount(self, prefix, adapter):  # noqa: ARG002
            return None

        def close(self):
            closed.append(self)

    monkeypatch.setattr("src.market.scanner.requests.Session", _FakeSession)

    s1 = scanner._get_gamma_requests_session()
    s2 = scanner._get_gamma_requests_session()

    assert s1 is s2
    assert len(created) == 1

    scanner._close_gamma_requests_sessions()

    assert closed == [s1]
    assert scanner._gamma_sessions == set()


def test_fetch_event_slug_markets_records_per_source_slug_stats(monkeypatch):
    scanner = MarketScanner(_config())
    closed = []

    class _Resp:
        def __init__(self, payload, slug):
            self.status_code = 200
            self._payload = payload
            self._slug = slug

        def json(self):
            return self._payload

        def close(self):
            closed.append(self._slug)

    def _fake_gamma_get(path, params=None, timeout=None):  # noqa: ARG001
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
                ],
                slug,
            )
        return _Resp([], slug)

    monkeypatch.setattr(scanner, "_gamma_get", _fake_gamma_get)

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
    assert sorted(closed) == ["btc-updown-15m-hit", "btc-updown-15m-miss", "btc-updown-15m-miss"]


def test_empty_slug_cache_skips_repeat_lookup_within_cycle(monkeypatch):
    scanner = MarketScanner(_config())
    calls = {"count": 0}

    class _Resp:
        status_code = 200

        def json(self):
            return []

        def close(self):
            return None

    def _fake_gamma_get(path, params=None, timeout=None):  # noqa: ARG001
        calls["count"] += 1
        return _Resp()

    monkeypatch.setattr(scanner, "_gamma_get", _fake_gamma_get)

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
    closed = []

    def _fake_gamma_get(path, params=None, timeout=None):  # noqa: ARG001
        if "/markets" in path:
            class _MarketsResp:
                status_code = 200

                def json(self_inner):
                    return []

                def close(self_inner):
                    closed.append("markets")

            return _MarketsResp()
        if "/events" in path:
            class _EventsResp:
                status_code = 200

                def json(self_inner):
                    return [{"markets": [gm_payload]}]

                def close(self_inner):
                    closed.append("events")

            return _EventsResp()
        raise AssertionError(path)

    monkeypatch.setattr(scanner, "_gamma_get", _fake_gamma_get)

    markets = scanner._fetch_event_slug_markets(
        ["btc-updown-15m-1234567890"], timeout_sec=1
    )
    assert len(markets) == 1
    assert markets[0].id == "from-event"
    assert closed == ["markets", "events"]


def test_fetch_updown_markets_filters_to_fifteen_band(monkeypatch):
    """fetch_updown_markets returns only ~15m windows; the legacy 30m carry path is
    dropped since Polymarket discontinued the 30m crypto product family."""
    scanner = MarketScanner(_config())
    end = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    base = dict(
        description="",
        volume=0.0,
        liquidity=0.0,
        yes_price=0.5,
        no_price=0.5,
        spread=0.02,
        end_date=end,
        token_id_yes="y",
        token_id_no="n",
        group_item_title="",
    )
    raw = [
        Market(id="m15", question="Bitcoin 15m", slug="btc-updown-15m-1", window_minutes=12, **base),
        Market(id="m30", question="Bitcoin 30m window", slug="btc-updown-15m-2", window_minutes=30, **base),
        Market(id="m60", question="Long", slug="btc-updown-15m-3", window_minutes=60, **base),
        Market(id="mnone_bad", question="No wm", slug="foo-bar", window_minutes=None, **base),
        Market(id="mnone_15slug", question="No wm 15 slug", slug="sol-updown-15m-9", window_minutes=None, **base),
    ]

    def fake_fetch(slugs, *, timeout_sec=8, limit=None, stats_key=None):  # noqa: ARG001
        return list(raw)

    monkeypatch.setattr(scanner, "_fetch_event_slug_markets", fake_fetch)
    fifteen = scanner.fetch_updown_markets(look_ahead=1)
    assert {m.id for m in fifteen} == {"m15", "mnone_15slug"}
    # 30m, 60m, and unparseable rows are all dropped.
    assert "m30" not in {m.id for m in fifteen}
    assert "m60" not in {m.id for m in fifteen}
    assert "mnone_bad" not in {m.id for m in fifteen}


def test_dedupe_markets_by_id():
    from src.market.scanner import _dedupe_markets_by_id

    end = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    m = Market(
        id="x",
        question="q",
        description="",
        volume=0.0,
        liquidity=0.0,
        yes_price=0.5,
        no_price=0.5,
        spread=0.02,
        end_date=end,
        token_id_yes="y",
        token_id_no="n",
        group_item_title="",
        slug="s",
        window_minutes=15,
    )
    out = _dedupe_markets_by_id([m, m])
    assert len(out) == 1 and out[0].id == "x"
