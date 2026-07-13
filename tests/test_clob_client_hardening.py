"""
Tests for clob_client live-execution hardening:

1. L2 credential-expiry guard — derived Polymarket creds expire ~7 days after
   derivation with no rotation and no expiry signal. We track derivation time
   and refuse to place live orders once creds exceed `creds_max_age_hours`, so
   the failure is loud and pre-trade instead of a silent auth death on day ~8.

2. Order reconciliation fallback — /data/order/{id} drops filled/cancelled
   orders ("lies by omission"). get_order_status must fall back to /data/trades
   to recover a fill instead of stranding it as PENDING.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.clob_client import (
    CLOBClient,
    LEGACY_CLOB_CLIENT_LIVE_BLOCK_REASON,
    OrderStatus,
)


def _client(creds_max_age_hours: float = 144) -> CLOBClient:
    return CLOBClient(
        {
            "trading": {"dry_run": False},
            "polymarket": {"creds_max_age_hours": creds_max_age_hours},
        }
    )


# ── Change 1: credential expiry ──────────────────────────────────────────────


def test_credentials_age_none_before_set():
    c = _client()
    assert c.credentials_age() is None
    # Never-set creds can't be judged stale; client guards take over instead.
    assert c.credentials_expired() is False


def test_set_credentials_blocks_legacy_v1_without_v2(monkeypatch):
    c = _client()
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: False))

    assert LEGACY_CLOB_CLIENT_LIVE_BLOCK_REASON.startswith("Live CLOB execution is blocked")
    with pytest.raises(RuntimeError, match="Live CLOB execution is blocked"):
        c.set_credentials("private", "api", "secret", "passphrase")


@pytest.mark.asyncio
async def test_place_order_blocks_legacy_v1_without_v2(monkeypatch):
    c = _client()
    c.client = MagicMock()
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: False))

    order = await c.place_order(
        token_id="tok",
        side="BUY",
        price=0.5,
        size=5,
        dry_run=False,
    )

    assert order is None
    c.client.create_order.assert_not_called()


def test_credentials_fresh_after_set():
    c = _client()
    c._creds_set_at = datetime.now()
    assert c.credentials_expired() is False
    age = c.credentials_age()
    assert age is not None and age < timedelta(minutes=1)


def test_credentials_expired_past_max_age():
    c = _client(creds_max_age_hours=144)
    c._creds_set_at = datetime.now() - timedelta(hours=145)
    assert c.credentials_expired() is True


def test_credentials_not_expired_just_under_max_age():
    c = _client(creds_max_age_hours=144)
    c._creds_set_at = datetime.now() - timedelta(hours=143)
    assert c.credentials_expired() is False


@pytest.mark.asyncio
async def test_place_order_refuses_expired_credentials_when_cannot_refresh(monkeypatch):
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: True))
    c = _client(creds_max_age_hours=144)
    # py-clob client present but lacks re-derive methods → cannot self-heal.
    c.client = object()
    c._creds_set_at = datetime.now() - timedelta(hours=200)
    order = await c.place_order(
        token_id="tok",
        side="BUY",
        price=0.5,
        size=5,
        dry_run=False,
    )
    assert order is None  # refused, not placed


# ── Change 1b: L1 re-derive (self-heal) path ─────────────────────────────────


class _RederiveClient:
    """Stand-in py-clob-client that supports L2 credential re-derivation."""

    def __init__(self):
        self.set_creds_called_with = None
        self.derive_calls = 0

    def create_or_derive_api_creds(self):
        self.derive_calls += 1
        return {"apiKey": "k", "secret": "s", "passphrase": "p"}

    def set_api_creds(self, creds):
        self.set_creds_called_with = creds


@pytest.mark.asyncio
async def test_ensure_fresh_credentials_true_when_fresh():
    c = _client()
    c.client = _RederiveClient()
    c._creds_set_at = datetime.now()
    assert await c.ensure_fresh_credentials() is True
    # Fresh creds must not trigger a re-derive.
    assert c.client.derive_calls == 0


@pytest.mark.asyncio
async def test_ensure_fresh_credentials_rederives_when_expired():
    c = _client(creds_max_age_hours=144)
    fake = _RederiveClient()
    c.client = fake
    c._creds_set_at = datetime.now() - timedelta(hours=200)
    assert c.credentials_expired() is True

    assert await c.ensure_fresh_credentials() is True
    assert fake.derive_calls == 1
    assert fake.set_creds_called_with is not None
    # Expiry clock reset → no longer expired.
    assert c.credentials_expired() is False


@pytest.mark.asyncio
async def test_ensure_fresh_credentials_disabled_does_not_rederive():
    c = _client(creds_max_age_hours=144)
    c._auto_rederive_credentials = False
    fake = _RederiveClient()
    c.client = fake
    c._creds_set_at = datetime.now() - timedelta(hours=200)
    assert await c.ensure_fresh_credentials() is False
    assert fake.derive_calls == 0


@pytest.mark.asyncio
async def test_force_rederive_refreshes_even_when_fresh():
    # Startup bootstrap: force a re-derive regardless of tracked age, since
    # _creds_set_at only marks when we loaded the .env creds, not their real age.
    c = _client()
    fake = _RederiveClient()
    c.client = fake
    c._creds_set_at = datetime.now()  # "fresh" by age
    assert await c.ensure_fresh_credentials(force_rederive=True) is True
    assert fake.derive_calls == 1


@pytest.mark.asyncio
async def test_force_rederive_falls_back_to_staleness_when_derive_unavailable():
    # Forced re-derive can't run (no methods) but creds are still fresh by age →
    # must not refuse.
    c = _client()
    c.client = object()
    c._creds_set_at = datetime.now()
    assert await c.ensure_fresh_credentials(force_rederive=True) is True


@pytest.mark.asyncio
async def test_rederive_missing_methods_returns_false():
    c = _client()
    c.client = object()  # no create_or_derive_api_creds / set_api_creds
    c._creds_set_at = datetime.now() - timedelta(hours=200)
    assert await c._rederive_l2_credentials() is False


@pytest.mark.asyncio
async def test_place_order_self_heals_then_proceeds_past_cred_guard(monkeypatch):
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: True))
    # Expired creds + working re-derive → guard passes; order still returns None
    # here only because OrderArgs is unavailable in test env (py-clob not
    # installed), proving execution advanced *past* the credential check.
    c = _client(creds_max_age_hours=144)
    fake = _RederiveClient()
    c.client = fake
    c._creds_set_at = datetime.now() - timedelta(hours=200)
    await c.place_order(
        token_id="tok", side="BUY", price=0.5, size=5, dry_run=False
    )
    assert fake.derive_calls == 1  # self-heal fired before the order-type guard
    assert c.credentials_expired() is False


# ── Change 2: order reconciliation fallback ──────────────────────────────────


class _FakeClient:
    """Minimal stand-in for py-clob-client used in get_order_status tests."""

    def __init__(self, order_response, trades):
        self._order_response = order_response
        self._trades = trades

    def get_order(self, order_id):
        if isinstance(self._order_response, Exception):
            raise self._order_response
        return self._order_response

    def get_trades(self):
        return self._trades


@pytest.mark.asyncio
async def test_active_order_uses_order_endpoint():
    c = _client()
    c.client = _FakeClient({"status": "filled"}, trades=[])
    assert await c.get_order_status("o1") == OrderStatus.FILLED


# ── Change 3: public execution metadata wrappers ─────────────────────────────


class _FakePublicClient:
    def __init__(self):
        self.fee_calls = 0
        self.tick_calls = 0

    def get_fee_rate_bps(self, token_id):
        self.fee_calls += 1
        assert token_id == "tok"
        return 700

    def get_tick_size(self, token_id):
        self.tick_calls += 1
        assert token_id == "tok"
        return "0.001"


@pytest.mark.asyncio
async def test_fetch_taker_fee_rate_normalizes_bps_and_caches():
    c = _client()
    fake = _FakePublicClient()
    c._readonly_py_client = fake

    assert await c.fetch_taker_fee_rate("tok") == pytest.approx(0.07)
    assert await c.fetch_taker_fee_rate("tok") == pytest.approx(0.07)
    assert fake.fee_calls == 1


@pytest.mark.asyncio
async def test_fetch_tick_size_uses_public_client_and_caches():
    c = _client()
    fake = _FakePublicClient()
    c._readonly_py_client = fake

    assert await c.fetch_tick_size("tok") == "0.001"
    assert await c.fetch_tick_size("tok") == "0.001"
    assert fake.tick_calls == 1


@pytest.mark.asyncio
async def test_empty_order_response_reconciles_fill_from_trades():
    c = _client()
    # Order endpoint returns empty (filled order dropped); trade carries the id.
    c.client = _FakeClient({}, trades=[{"taker_order_id": "o1"}])
    assert await c.get_order_status("o1") == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_empty_order_response_no_trade_is_pending():
    c = _client()
    c.client = _FakeClient({}, trades=[{"taker_order_id": "other"}])
    # Resting/unmatched — not yet terminal.
    assert await c.get_order_status("o1") == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_order_endpoint_raises_falls_back_to_trades():
    c = _client()
    c.client = _FakeClient(KeyError("status"), trades=[{"order_id": "o1"}])
    assert await c.get_order_status("o1") == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_nested_maker_orders_reconcile():
    c = _client()
    c.client = _FakeClient(
        {}, trades=[{"maker_orders": [{"order_id": "o1"}]}]
    )
    assert await c.get_order_status("o1") == OrderStatus.FILLED


def test_quantize_price_for_tick_preserves_execution_intent():
    q = CLOBClient._quantize_price_for_tick

    assert q(0.421, "0.01", side="BUY", order_type="GTC") == pytest.approx(0.42)
    assert q(0.421, "0.01", side="SELL", order_type="GTC") == pytest.approx(0.43)
    assert q(0.421, "0.01", side="BUY", order_type="FAK") == pytest.approx(0.43)
    assert q(0.421, "0.01", side="SELL", order_type="FAK") == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_place_order_quantizes_price_before_signing(monkeypatch):
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: True))
    c = _client()
    c.client = MagicMock()
    c.client.create_order.return_value = "signed"
    c.client.post_order.return_value = {"order_id": "oid_tick"}
    c.ensure_fresh_credentials = AsyncMock(return_value=True)
    c.fetch_tick_size = AsyncMock(return_value="0.01")

    order = await c.place_order(
        token_id="tok",
        side="BUY",
        price=0.421,
        size=5,
        dry_run=False,
        order_type="GTC",
    )

    assert order is not None
    order_args = c.client.create_order.call_args.args[0]
    assert order_args.price == pytest.approx(0.42)
    assert order.price == pytest.approx(0.42)


class _PostErrorTradeClient:
    def create_order(self, order_args):
        return "signed"

    def post_order(self, signed_order, order_type, post_only):
        raise RuntimeError("400 Bad Request")

    def get_trades(self, params=None):
        return [
            {
                "asset_id": "tok",
                "taker_order_id": "oid_recovered",
                "price": 0.42,
                "size": 5,
            }
        ]


@pytest.mark.asyncio
async def test_place_order_recovers_recent_trade_after_post_error(monkeypatch):
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: True))
    c = _client()
    c.client = _PostErrorTradeClient()
    c.ensure_fresh_credentials = AsyncMock(return_value=True)
    c.fetch_tick_size = AsyncMock(return_value="0.01")

    order = await c.place_order(
        token_id="tok",
        side="SELL",
        price=0.421,
        size=5,
        dry_run=False,
        order_type="FAK",
    )

    assert order is not None
    assert order.order_id == "oid_recovered"
    assert order.status == OrderStatus.FILLED
    assert order.price == pytest.approx(0.42)


# ── place_entry_order: maker/taker/hybrid policy ─────────────────────────────


def _entry_client():
    c = CLOBClient({"polymarket": {}})
    return c


def _fresh_fill_entry_client():
    return CLOBClient(
        {
            "polymarket": {},
            "trading": {
                "paper_entry_fresh_fill": True,
                "paper_entry_fresh_fill_slippage_tol": 0.03,
            },
        }
    )


def _fake_order(oid="oid", status=OrderStatus.PENDING, filled=0.0):
    from src.execution.clob_client import Order
    return Order(
        order_id=oid, market_id="m", token_id="t", side="BUY", outcome="YES",
        price=0.5, size=10.0, filled_size=filled, status=status,
    )


@pytest.mark.asyncio
async def test_entry_dry_run_is_marketable_taker_regardless_of_mode():
    c = _entry_client()
    c.place_order = AsyncMock(return_value=_fake_order(status=OrderStatus.FILLED))
    await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=True, entry_mode="hybrid", maker_wait_sec=0,
    )
    kw = c.place_order.call_args.kwargs
    assert kw["dry_run"] is True and kw["order_type"] == "FAK" and kw["post_only"] is False


@pytest.mark.asyncio
async def test_entry_dry_run_fresh_fill_uses_executable_ask_within_smoke_tolerance():
    c = _fresh_fill_entry_client()
    c.fetch_order_book_snapshot = AsyncMock(
        return_value={"asks": [{"price": "0.52", "size": "25"}]}
    )
    c.place_order = AsyncMock(return_value=_fake_order(status=OrderStatus.FILLED))

    await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=True, entry_mode="marketable", maker_wait_sec=0,
    )

    kw = c.place_order.call_args.kwargs
    assert kw["price"] == pytest.approx(0.52)
    assert kw["size"] == pytest.approx(10.0)
    assert kw["order_type"] == "FAK" and kw["post_only"] is False


@pytest.mark.asyncio
async def test_entry_dry_run_fresh_fill_rejects_asks_outside_smoke_tolerance():
    c = _fresh_fill_entry_client()
    c.fetch_order_book_snapshot = AsyncMock(
        return_value={"asks": [{"price": "0.54", "size": "25"}]}
    )
    c.place_order = AsyncMock(return_value=_fake_order(status=OrderStatus.FILLED))

    out = await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=True, entry_mode="marketable", maker_wait_sec=0,
    )

    assert out is None
    c.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_dry_run_fresh_fill_records_partial_executable_size():
    c = _fresh_fill_entry_client()
    c.fetch_order_book_snapshot = AsyncMock(
        return_value={"asks": [{"price": "0.51", "size": "6"}]}
    )
    c.place_order = AsyncMock(return_value=_fake_order(status=OrderStatus.FILLED))

    await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=True, entry_mode="marketable", maker_wait_sec=0,
    )

    kw = c.place_order.call_args.kwargs
    assert kw["price"] == pytest.approx(0.51)
    assert kw["size"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_entry_marketable_live_uses_fak_taker():
    c = _entry_client()
    c.place_order = AsyncMock(return_value=_fake_order())
    await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=False, entry_mode="marketable", maker_wait_sec=0,
    )
    kw = c.place_order.call_args.kwargs
    assert kw["order_type"] == "FAK" and kw["post_only"] is False
    assert c.place_order.await_count == 1


@pytest.mark.asyncio
async def test_entry_maker_live_uses_post_only_gtc():
    c = _entry_client()
    c.place_order = AsyncMock(return_value=_fake_order())
    await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=False, entry_mode="maker", maker_wait_sec=0,
    )
    kw = c.place_order.call_args.kwargs
    assert kw["order_type"] == "GTC" and kw["post_only"] is True
    assert c.place_order.await_count == 1


@pytest.mark.asyncio
async def test_entry_hybrid_5m_falls_back_to_marketable():
    c = _entry_client()
    c.place_order = AsyncMock(return_value=_fake_order())
    await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="5m",
        dry_run=False, entry_mode="hybrid", maker_wait_sec=0,
        hybrid_windows=("15m", "1h"),
    )
    kw = c.place_order.call_args.kwargs
    assert kw["order_type"] == "FAK" and kw["post_only"] is False
    assert c.place_order.await_count == 1  # no maker leg on 5m


@pytest.mark.asyncio
async def test_entry_hybrid_15m_maker_fills_no_taker():
    c = _entry_client()
    c.place_order = AsyncMock(return_value=_fake_order(oid="maker1"))
    c.get_order_status = AsyncMock(return_value=OrderStatus.FILLED)
    c.cancel_order = AsyncMock(return_value=True)
    out = await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=False, entry_mode="hybrid", maker_wait_sec=0,
    )
    # maker leg only; never cancelled, never crossed
    assert c.place_order.await_count == 1
    assert c.place_order.call_args.kwargs["post_only"] is True
    c.cancel_order.assert_not_awaited()
    assert out.order_id == "maker1"


@pytest.mark.asyncio
async def test_entry_hybrid_15m_unfilled_crosses_to_taker():
    c = _entry_client()
    c.place_order = AsyncMock(side_effect=[_fake_order(oid="maker1"), _fake_order(oid="taker1")])
    c.get_order_status = AsyncMock(return_value=OrderStatus.PENDING)
    c.cancel_order = AsyncMock(return_value=True)
    out = await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="1h",
        dry_run=False, entry_mode="hybrid", maker_wait_sec=0,
    )
    assert c.place_order.await_count == 2
    first, second = c.place_order.call_args_list
    assert first.kwargs["post_only"] is True and first.kwargs["order_type"] == "GTC"
    assert second.kwargs["post_only"] is False and second.kwargs["order_type"] == "FAK"
    c.cancel_order.assert_awaited_once()
    assert out.order_id == "taker1"


@pytest.mark.asyncio
async def test_entry_hybrid_partial_keeps_partial_no_double_fill():
    c = _entry_client()
    c.place_order = AsyncMock(return_value=_fake_order(oid="maker1"))
    c.get_order_status = AsyncMock(return_value=OrderStatus.PARTIAL)
    c.cancel_order = AsyncMock(return_value=True)
    out = await c.place_entry_order(
        token_id="t", side="BUY", price=0.5, size=10, window="15m",
        dry_run=False, entry_mode="hybrid", maker_wait_sec=0,
    )
    # cancelled the remainder but did NOT place a second (taker) order
    assert c.place_order.await_count == 1
    c.cancel_order.assert_awaited_once()
    assert out.order_id == "maker1"
