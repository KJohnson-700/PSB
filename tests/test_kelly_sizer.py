from src.analysis.kelly_sizer import KellySizer


def test_size_from_edge_uses_configured_floor_and_ceiling():
    sizer = KellySizer(
        {
            "trading": {
                "default_position_size": 10,
                "max_position_size": 15,
                "max_exposure_per_trade": 0.05,
            },
            "strategies": {"bitcoin": {"kelly_fraction": 0.30}},
        }
    )

    assert sizer.size_from_edge("bitcoin", bankroll=500, edge=0.01) == 10.0
    assert sizer.size_from_edge("bitcoin", bankroll=500, edge=0.50) == 15.0


def test_size_from_edge_bankroll_cap_wins_over_floor_for_small_bankroll():
    sizer = KellySizer(
        {
            "trading": {
                "default_position_size": 10,
                "max_position_size": 15,
                "max_exposure_per_trade": 0.05,
            },
            "strategies": {"bitcoin": {"kelly_fraction": 0.30}},
        }
    )

    assert sizer.size_from_edge("bitcoin", bankroll=100, edge=0.20) == 5.0


def test_binary_position_uses_same_configured_floor_and_ceiling():
    sizer = KellySizer(
        {
            "trading": {
                "default_position_size": 10,
                "max_position_size": 15,
                "max_exposure_per_trade": 0.05,
            },
            "strategies": {"weather": {"kelly_fraction": 0.25}},
        }
    )

    assert sizer.size_binary_position("weather", bankroll=500, win_probability=0.60, contract_price=0.50) == 15.0


def test_get_all_window_stats_includes_30m_bucket():
    sizer = KellySizer({"trading": {}, "strategies": {}})
    ws = sizer.get_all_window_stats()
    assert set(ws["bitcoin"].keys()) >= {"5m", "15m", "30m"}
    assert ws["bitcoin"]["30m"]["trades"] == 0

