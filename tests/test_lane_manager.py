from src.analysis.lane_manager import LaneManager


def test_lane_manager_blocks_paused_lane() -> None:
    mgr = LaneManager(
        {
            "lane_management": {
                "enabled": True,
                "default_state": "paper",
                "states": {
                    "bitcoin|5m|down": "paused",
                },
            }
        }
    )
    allowed, reason, state, matched = mgr.can_execute(
        "bitcoin|5m|down|bearish|drift",
        dry_run=True,
    )
    assert allowed is False
    assert reason == "lane_paused"
    assert state == "paused"
    assert matched == "bitcoin|5m|down"


def test_lane_manager_blocks_paper_lane_in_live_mode() -> None:
    mgr = LaneManager(
        {
            "lane_management": {
                "enabled": True,
                "default_state": "paper",
                "states": {
                    "eth_macro|15m|up": "paper",
                },
            }
        }
    )
    allowed, reason, state, matched = mgr.can_execute(
        "eth_macro|15m|up|bullish|standard",
        dry_run=False,
    )
    assert allowed is False
    assert reason == "lane_paper_only"
    assert state == "paper"
    assert matched == "eth_macro|15m|up"


def test_lane_manager_allows_live_lane_in_paper_and_live_mode() -> None:
    mgr = LaneManager(
        {
            "lane_management": {
                "enabled": True,
                "default_state": "paper",
                "states": {
                    "xrp_macro|15m|up": "live",
                },
            }
        }
    )
    allowed_paper, reason_paper, state_paper, _ = mgr.can_execute(
        "xrp_macro|15m|up|bullish|standard",
        dry_run=True,
    )
    allowed_live, reason_live, state_live, _ = mgr.can_execute(
        "xrp_macro|15m|up|bullish|standard",
        dry_run=False,
    )
    assert allowed_paper is True
    assert reason_paper == "lane_allowed"
    assert state_paper == "live"
    assert allowed_live is True
    assert reason_live == "lane_allowed"
    assert state_live == "live"


def test_lane_manager_recommends_live_on_good_sample() -> None:
    mgr = LaneManager({"lane_management": {}})
    rec, reasons = mgr.recommend_state(
        {
            "trades": 20,
            "win_rate": 0.60,
            "expectancy": 0.75,
            "edge_realized_gap": 0.02,
        }
    )
    assert rec == "live"
    assert reasons


def test_lane_manager_recommends_paused_on_bad_sample() -> None:
    mgr = LaneManager({"lane_management": {}})
    rec, reasons = mgr.recommend_state(
        {
            "trades": 10,
            "win_rate": 0.30,
            "expectancy": -1.0,
            "edge_realized_gap": 0.12,
        }
    )
    assert rec == "paused"
    assert reasons


def test_lane_manager_marks_auto_pause_candidate_for_live_lane() -> None:
    mgr = LaneManager(
        {
            "lane_management": {
                "states": {"bitcoin|5m|down": "live"},
                "recommendations": {
                    "min_pause_trades": 8,
                    "max_pause_win_rate": 0.40,
                    "max_pause_expectancy": -0.5,
                    "min_pause_gap": 0.08,
                    "auto_pause_confirmation_trades": 3,
                },
            }
        }
    )
    assessment = mgr.assess_lane(
        "bitcoin|5m|down|bearish|drift",
        {
            "trades": 9,
            "win_rate": 0.20,
            "expectancy": -1.0,
            "edge_realized_gap": 0.12,
        },
    )
    assert assessment["recommended_state"] == "paused"
    assert assessment["auto_pause_candidate"] is True
    assert assessment["auto_pause_confirmed"] is False
    assert assessment["auto_pause_confirmation_remaining"] == 2


def test_lane_manager_marks_auto_pause_confirmed_after_window() -> None:
    mgr = LaneManager(
        {
            "lane_management": {
                "states": {"bitcoin|5m|down": "live"},
                "recommendations": {"auto_pause_confirmation_trades": 3},
            }
        }
    )
    assessment = mgr.assess_lane(
        "bitcoin|5m|down|bearish|drift",
        {
            "trades": 11,
            "win_rate": 0.20,
            "expectancy": -1.0,
            "edge_realized_gap": 0.12,
        },
    )
    assert assessment["recommended_state"] == "paused"
    assert assessment["auto_pause_candidate"] is True
    assert assessment["auto_pause_confirmed"] is True
    assert assessment["auto_pause_confirmation_remaining"] == 0
