"""Guards the 2026-06-17 `buy_no_<tf>_allow_postflip` loosening.

The window-delta-FLIP short is the tape-driven off-bias edge (sol 1h flip +0.220
EV vs native −0.344). `allow_postflip` admits that flip short while the native
side stays disabled by `disable_buy_no_<tf>`.
"""
from src.strategies.sol_macro import SolMacroStrategy


def _strat(cfg):
    s = SolMacroStrategy.__new__(SolMacroStrategy)
    s.config = cfg
    return s


FLIP = "window_delta_flip"


def test_postflip_suppressed_without_allow_flag():
    s = _strat({"disable_buy_no_1h": True})
    assert s._post_flip_disabled_side("BUY_NO", "1h", FLIP) == "buy_no_1h_disabled_lane_postflip"


def test_allow_postflip_admits_flip_short():
    s = _strat({"disable_buy_no_1h": True, "buy_no_1h_allow_postflip": True})
    # Flip short is admitted (returns None = no skip) even though the lane is disabled.
    assert s._post_flip_disabled_side("BUY_NO", "1h", FLIP) is None


def test_allow_postflip_does_not_touch_native_side():
    # Native (non-flip) candidates never reach the post-flip recheck; the native
    # disable still applies upstream. The recheck only fires for flip side_source.
    s = _strat({"disable_buy_no_1h": True, "buy_no_1h_allow_postflip": True})
    assert s._post_flip_disabled_side("BUY_NO", "1h", "native") is None
    assert s._post_flip_disabled_side("BUY_NO", "1h", None) is None


def test_allow_postflip_is_per_tf():
    # 1h allow flag must NOT leak into 15m (15m shorts are correctly -EV / disabled).
    s = _strat({"disable_buy_no_15m": True, "buy_no_1h_allow_postflip": True})
    assert s._post_flip_disabled_side("BUY_NO", "15m", FLIP) == "buy_no_15m_disabled_lane_postflip"
