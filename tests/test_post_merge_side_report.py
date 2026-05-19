import json

from tools.post_merge_side_report import build_report


def test_post_merge_side_report_groups_and_classifies(tmp_path):
    trades = tmp_path / "trades.jsonl"
    trades.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-05-17T12:00:00Z",
                        "closed_at": "2026-05-17T12:30:00Z",
                        "strategy": "eth_macro",
                        "window": "15m",
                        "side": "BUY_YES",
                        "lane_family": "standard",
                        "side_source": "bullish_rally_default",
                        "entry_price_bucket": "0.49_0.51",
                        "regime_tag_bucket": "bullish",
                        "win": True,
                        "pnl": 1.2,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-05-17T13:00:00Z",
                        "closed_at": "2026-05-17T13:15:00Z",
                        "strategy": "eth_macro",
                        "window": "15m",
                        "side": "BUY_YES",
                        "lane_family": "standard",
                        "side_source": "bullish_rally_default",
                        "entry_price_bucket": "0.49_0.51",
                        "regime_tag_bucket": "bullish",
                        "win": False,
                        "pnl": -0.5,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rejected = tmp_path / "rejected.jsonl"
    rejected.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-05-17T14:00:00Z",
                        "strategy": "eth_macro",
                        "window": "15m",
                        "action": "BUY_YES",
                        "reason": "lane_min_edge",
                        "lane_family": "standard",
                        "side_source": "bullish_rally_default",
                        "entry_price_bucket": "0.49_0.51",
                        "regime_tag_bucket": "bullish",
                        "win": True,
                        "realized_pct": 0.7,
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_report(
        trades_path=trades,
        rejected_path=rejected,
        since="2026-05-17T11:11:00Z",
    )

    assert report["taken_trades"][0]["strategy"] == "eth_macro"
    assert report["taken_trades"][0]["win_rate"] == 0.5
    assert report["rejected_ghosts"][0]["reason"] == "lane_min_edge"
    assert report["side_selection_counts"][0]["chosen_side"] == "BUY_YES"
    assert report["buy_yes_lane_checks"][0]["state"] in {
        "under-sampled",
        "over-blocked",
        "over-admitted",
        "healthy",
    }
