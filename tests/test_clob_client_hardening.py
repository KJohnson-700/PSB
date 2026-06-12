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

import pytest

from src.execution.clob_client import CLOBClient, OrderStatus


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
async def test_place_order_refuses_expired_credentials_when_cannot_refresh():
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
async def test_place_order_self_heals_then_proceeds_past_cred_guard():
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
