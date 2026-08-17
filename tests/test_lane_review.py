import importlib.util
from pathlib import Path


def _load_lane_review():
    path = Path(__file__).resolve().parents[1] / "scripts" / "lane_review.py"
    spec = importlib.util.spec_from_file_location("lane_review", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_market_favorite_loss_is_policy_bleed_not_native_cut():
    lr = _load_lane_review()
    row = {
        "strategy": "eth_macro",
        "action": "BUY_NO",
        "entry_price": 0.52,
        "pnl": -20.0,
        "extra": {
            "lane_window": "5m",
            "signal_reason": "side_src=eth_5m_native+market_favorite",
        },
    }

    key = lr._lane_key(row, include_source=True)
    flag = lr._flag(key, lr.stats([row] * 4))

    assert key == "eth_macro|5m|BUY_NO|market_favorite"
    assert "POLICY_BLEED" in flag
    assert "REVIEW_CUT" not in flag


def test_native_loss_below_watchlist_n_accrues_before_cut():
    lr = _load_lane_review()
    row = {
        "strategy": "eth_macro",
        "action": "BUY_NO",
        "entry_price": 0.52,
        "pnl": -20.0,
        "extra": {"lane_window": "5m", "signal_reason": "side_src=eth_5m_native"},
    }

    key = lr._lane_key(row, include_source=True)
    flag = lr._flag(key, lr.stats([row] * 10))

    assert key == "eth_macro|5m|BUY_NO|resolver_native"
    assert "ACCRUE(10/45)" in flag
    assert "REVIEW_CUT" not in flag
