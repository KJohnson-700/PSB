import json
from datetime import datetime, timedelta

from src.market.price_collector import PriceCollector
from src.market.scanner import Market


def test_price_collector_writes_jsonl(tmp_path):
    out = tmp_path / "weather_prices.jsonl"
    cfg = {
        "data_collection": {
            "weather": {
                "enabled": True,
                "interval_sec": 0,
                "path": str(out),
            }
        }
    }
    collector = PriceCollector(cfg)
    market = Market(
        id="m1",
        question="Highest temperature in Manila on Apr 29, 2026?",
        description="highest-temperature-in-manila-on-apr-29-2026",
        volume=1234.0,
        liquidity=5678.0,
        yes_price=0.42,
        no_price=0.58,
        spread=0.03,
        end_date=datetime.utcnow() + timedelta(hours=12),
        token_id_yes="yes1",
        token_id_no="no1",
        group_item_title="manila",
        slug="highest-temperature-in-manila-on-apr-29-2026",
    )
    collector.maybe_collect([market])
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["market_id"] == "m1"
    assert row["slug"] == "highest-temperature-in-manila-on-apr-29-2026"
    assert row["yes_price"] == 0.42
