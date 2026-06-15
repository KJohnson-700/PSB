from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from src.execution.clob_client import CLOBClient, OrderStatus
from src.execution.olympus_client import OlympusClient


def test_build_buy_yes_payload():
    payload = OlympusClient.build_trade_payload(
        token_id="yes-token",
        side="BUY",
        price=0.62,
        size=40.3225806,
        market_id="540817",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
        outcome_label="Yes",
    )

    assert payload == {
        "side": "BUY",
        "tokenId": "yes-token",
        "conditionId": "0xcondition",
        "amountUsd": 25.0,
        "maxPrice": 0.62,
        "marketTitle": "Example market?",
        "marketId": "540817",
        "marketSlug": "example-market",
        "outcomeLabel": "Yes",
    }


def test_build_buy_no_payload():
    payload = OlympusClient.build_trade_payload(
        token_id="no-token",
        side="BUY",
        price=0.45,
        size=55.555555,
        market_id="540817",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
        outcome_label="No",
    )

    assert payload["tokenId"] == "no-token"
    assert payload["outcomeLabel"] == "No"
    assert payload["amountUsd"] == pytest.approx(25.0)
    assert payload["maxPrice"] == 0.45


def test_build_sell_payload_by_shares():
    payload = OlympusClient.build_trade_payload(
        token_id="held-token",
        side="SELL",
        price=0.55,
        size=25,
        market_id="540817",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
    )

    assert payload == {
        "side": "SELL",
        "tokenId": "held-token",
        "conditionId": "0xcondition",
        "sellSpec": {"type": "shares", "sharesNormalized": 25.0},
        "minPrice": 0.55,
        "marketTitle": "Example market?",
        "marketId": "540817",
        "marketSlug": "example-market",
    }


def test_olympus_safe_error_body_redacts_identifiers():
    raw = (
        '{"status":"FAILED","errorCode":"NO_ORDERBOOK_LIQUIDITY",'
        '"message":"trade tr_secret123 rejected for token '
        '123456789012345678901234567890 and wallet '
        '0x1234567890abcdef1234567890abcdef12345678"}'
    )

    redacted = OlympusClient.safe_error_body(raw)

    assert "NO_ORDERBOOK_LIQUIDITY" in redacted
    assert "tr_secret123" not in redacted
    assert "123456789012345678901234567890" not in redacted
    assert "0x1234567890abcdef1234567890abcdef12345678" not in redacted
    assert "tr_<redacted>" in redacted
    assert "0x<redacted>" in redacted


@pytest.mark.asyncio
async def test_submit_trade_requires_live_approval():
    client = OlympusClient({"olympus": {"api_key": "secret"}})

    with pytest.raises(RuntimeError, match="live order blocked"):
        await client.submit_trade({"side": "BUY"})


@pytest.mark.asyncio
async def test_smoke_test_blocks_order_above_cap():
    client = OlympusClient(
        {
            "trading": {"dry_run": False},
            "olympus": {
                "api_key": "secret",
                "live_order_approved": True,
                "smoke_test": {"enabled": True, "max_order_usd": 5},
            },
        }
    )
    payload = OlympusClient.build_trade_payload(
        token_id="yes-token",
        side="BUY",
        price=0.5,
        size=12,
        market_id="540817",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
        outcome_label="Yes",
    )

    with pytest.raises(RuntimeError, match="exceeds cap"):
        await client.submit_trade(payload)


@pytest.mark.asyncio
async def test_smoke_test_one_shot_counter(monkeypatch):
    client = OlympusClient(
        {
            "trading": {"dry_run": False},
            "olympus": {
                "api_key": "secret",
                "live_order_approved": True,
                "smoke_test": {"enabled": True, "max_order_usd": 5, "max_orders_per_run": 1},
            },
        }
    )
    client._request_json = lambda method, path, payload=None: {
        "tradeId": "tr_1",
        "status": "QUEUED",
    }
    payload = OlympusClient.build_trade_payload(
        token_id="yes-token",
        side="BUY",
        price=0.5,
        size=2,
        market_id="540817",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
        outcome_label="Yes",
    )

    first = await client.submit_trade(payload)
    assert first.trade_id == "tr_1"
    with pytest.raises(RuntimeError, match="order limit reached"):
        await client.submit_trade(payload)


@pytest.mark.asyncio
async def test_smoke_test_guard_blocks_paper_mode_routing_bug():
    """Defense-in-depth: _enforce_smoke_limits must raise in dry_run (paper).

    CLOBClient.place_order short-circuits paper fills before reaching Olympus, so
    this guard should never run in paper. If it does, the routing layer is broken
    and paper fills would be silently capped at the live smoke cap — fail loud.
    """
    client = OlympusClient(
        {
            "trading": {"dry_run": True},
            "olympus": {
                "api_key": "secret",
                "live_order_approved": True,
                "smoke_test": {"enabled": True, "max_order_usd": 6},
            },
        }
    )
    payload = OlympusClient.build_trade_payload(
        token_id="yes-token",
        side="BUY",
        price=0.5,
        size=20,
        market_id="540817",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
        outcome_label="Yes",
    )
    with pytest.raises(RuntimeError, match="dry_run"):
        await client.submit_trade(payload)


@pytest.mark.asyncio
async def test_clob_client_routes_live_order_to_olympus(monkeypatch):
    client = CLOBClient(
        {
            "trading": {"dry_run": False, "execution_provider": "olympus"},
            "olympus": {"api_key": "secret", "live_order_approved": True},
        }
    )
    client.olympus_client.submit_trade = AsyncMock(
        return_value=type("Resp", (), {"trade_id": "tr_1", "status": "QUEUED"})()
    )

    order = await client.place_order(
        token_id="yes-token",
        side="BUY",
        price=0.62,
        size=40.3225806,
        market_id="540817",
        dry_run=False,
        order_outcome="YES",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
    )

    assert order is not None
    assert order.order_id == "tr_1"
    assert order.status == OrderStatus.PENDING
    payload = client.olympus_client.submit_trade.call_args.args[0]
    assert payload["amountUsd"] == 25.0
    assert payload["outcomeLabel"] == "Yes"


@pytest.mark.asyncio
async def test_clob_client_smoke_awaits_olympus_fill_and_records_execution():
    client = CLOBClient(
        {
            "trading": {"dry_run": False, "execution_provider": "olympus"},
            "olympus": {
                "api_key": "secret",
                "live_order_approved": True,
                "await_fill_on_submit": True,
                "fill_poll_attempts": 1,
                "fill_poll_interval_sec": 0,
                "smoke_test": {"enabled": True, "max_order_usd": 5},
            },
        }
    )
    client.olympus_client.submit_trade = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "trade_id": "tr_1",
                "status": "QUEUED",
                "raw": {"status": "QUEUED"},
            },
        )()
    )
    client.olympus_client.get_trade_status = AsyncMock(
        return_value={
            "status": "SUCCEEDED",
            "filledPrice": 0.61,
            "filledSharesNormalized": 8.0,
            "spentUsd": 4.88,
            "feeUsd": 0.05,
        }
    )

    order = await client.place_order(
        token_id="yes-token",
        side="BUY",
        price=0.62,
        size=8.0,
        market_id="540817",
        dry_run=False,
        order_outcome="YES",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
    )

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.price == pytest.approx(0.61)
    assert order.filled_size == pytest.approx(8.0)
    assert order.execution["execution_provider"] == "olympus"
    assert order.execution["olympus_status"] == "SUCCEEDED"
    assert order.execution["olympus_filled_price"] == pytest.approx(0.61)
    assert order.execution["olympus_price_delta"] == pytest.approx(-0.01)
    assert order.execution["olympus_fee_usdc"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_clob_client_smoke_failed_olympus_fill_returns_none(caplog):
    client = CLOBClient(
        {
            "trading": {"dry_run": False, "execution_provider": "olympus"},
            "olympus": {
                "api_key": "secret",
                "live_order_approved": True,
                "await_fill_on_submit": True,
                "fill_poll_attempts": 1,
                "fill_poll_interval_sec": 0,
                "smoke_test": {"enabled": True, "max_order_usd": 5},
            },
        }
    )
    client.olympus_client.submit_trade = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "trade_id": "tr_sensitive123",
                "status": "QUEUED",
                "raw": {"status": "QUEUED"},
            },
        )()
    )
    client.olympus_client.get_trade_status = AsyncMock(
        return_value={
            "status": "FAILED",
            "errorCode": "NO_ORDERBOOK_LIQUIDITY",
            "message": (
                "trade tr_sensitive123 rejected for token "
                "123456789012345678901234567890"
            ),
        }
    )

    caplog.set_level(logging.ERROR, logger="src.execution.clob_client")
    order = await client.place_order(
        token_id="yes-token",
        side="BUY",
        price=0.62,
        size=8.0,
        market_id="540817",
        dry_run=False,
        order_outcome="YES",
        market_title="Example market?",
        market_slug="example-market",
        condition_id="0xcondition",
    )

    assert order is None
    failed = client.pending_orders["tr_sensitive123"]
    assert failed.execution["olympus_status"] == "FAILED"
    assert failed.execution["olympus_failure_code"] == "NO_ORDERBOOK_LIQUIDITY"
    assert "tr_sensitive123" not in failed.execution["olympus_failure_reason"]
    assert "123456789012345678901234567890" not in failed.execution["olympus_failure_reason"]
    assert "NO_ORDERBOOK_LIQUIDITY" in caplog.text
    assert "tr_sensitive123" not in caplog.text
    assert "123456789012345678901234567890" not in caplog.text


@pytest.mark.asyncio
async def test_clob_client_olympus_status_mapping():
    client = CLOBClient(
        {
            "trading": {"dry_run": False, "execution_provider": "olympus"},
            "olympus": {"api_key": "secret"},
        }
    )
    client.olympus_client.get_trade_status = AsyncMock(return_value={"status": "SUCCEEDED"})
    assert await client.get_order_status("tr_1") == OrderStatus.FILLED

    client.olympus_client.get_trade_status = AsyncMock(return_value={"status": "FAILED"})
    assert await client.get_order_status("tr_2") == OrderStatus.FAILED

    client.olympus_client.get_trade_status = AsyncMock(return_value={"status": "PROCESSING"})
    assert await client.get_order_status("tr_3") == OrderStatus.PENDING
