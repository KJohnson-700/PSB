from __future__ import annotations

import pytest

from src.market.websocket import WebSocketClient


@pytest.mark.asyncio
async def test_market_book_event_uses_asset_id_and_replaces_snapshot():
    client = WebSocketClient({})

    await client._handle_message(
        {
            "event_type": "book",
            "asset_id": "asset-1",
            "bids": [
                {"price": "0.40", "size": "10"},
                {"price": "0.44", "size": "4"},
            ],
            "asks": [
                {"price": "0.57", "size": "3"},
                {"price": "0.52", "size": "5"},
            ],
            "tick_size": "0.01",
            "hash": "snapshot-1",
        }
    )

    book = client.get_order_book("asset-1")
    assert book is not None
    assert book.best_bid == 0.44
    assert book.best_ask == 0.52
    assert book.mid_price == pytest.approx(0.48)
    assert book.last_update > 0

    await client._handle_message(
        {
            "event_type": "book",
            "asset_id": "asset-1",
            "bids": [{"price": "0.41", "size": "2"}],
            "asks": [{"price": "0.55", "size": "7"}],
        }
    )

    book = client.get_order_book("asset-1")
    assert book is not None
    assert book.bids == [{"price": 0.41, "size": 2.0}]
    assert book.asks == [{"price": 0.55, "size": 7.0}]


@pytest.mark.asyncio
async def test_market_price_change_event_merges_delta_from_asset_id():
    client = WebSocketClient({})

    await client._handle_message(
        {
            "event_type": "book",
            "asset_id": "asset-1",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "10"}],
        }
    )

    await client._handle_message(
        {
            "event_type": "price_change",
            "asset_id": "asset-1",
            "bids": [{"price": "0.42", "size": "9"}],
            "asks": [{"price": "0.60", "size": "0"}],
        }
    )

    book = client.get_order_book("asset-1")
    assert book is not None
    assert book.bids == [
        {"price": 0.42, "size": 9.0},
        {"price": 0.40, "size": 10.0},
    ]
    assert book.asks == []


@pytest.mark.asyncio
async def test_market_price_change_event_accepts_documented_price_changes_array():
    client = WebSocketClient({})

    await client._handle_message(
        {
            "event_type": "book",
            "asset_id": "asset-1",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "10"}],
        }
    )

    await client._handle_message(
        {
            "event_type": "price_change",
            "asset_id": "asset-1",
            "price_changes": [
                {"price": "0.43", "size": "12", "side": "BUY"},
                {"price": "0.60", "size": "0", "side": "SELL"},
            ],
        }
    )

    book = client.get_order_book("asset-1")
    assert book is not None
    assert book.bids[0] == {"price": 0.43, "size": 12.0}
    assert book.asks == []
