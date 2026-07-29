"""Unit tests for LaneTapeAdapter — winner protection, loser de-size, symmetry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.analysis.lane_tape_adapter import LaneTapeAdapter, lane_key  # noqa: E402

LIVE = {
    "mode": "live", "window_closes": 5, "green_arm_pct": 0.08, "min_samples": 2,
    "min_mult": 0.25, "max_mult": 1.0, "loss_ref_dollars": 4.0,
    "green_keep_rate": 0.5, "recency_ramp": 2.0,
}


def test_lane_key_normalizes_side():
    assert lane_key("xrp_macro", "5m", "BUY_YES") == "xrp|5m|up"
    assert lane_key("xrp_macro", "5m", "BUY_NO") == "xrp|5m|down"
    assert lane_key("DOGE", "15m", "short") == "doge|15m|down"


def test_warmup_returns_neutral():
    ad = LaneTapeAdapter(LIVE)
    # no data -> 1.0
    assert ad.size_multiplier("xrp", "5m", "up") == 1.0
    ad.record_close("xrp", "5m", "up", mfe_pct=0.0, pnl=-5.0)
    # still below min_samples=2 -> neutral
    assert ad.size_multiplier("xrp", "5m", "up") == 1.0


def test_off_and_shadow_never_change_size():
    for mode in ("off", "shadow"):
        ad = LaneTapeAdapter({**LIVE, "mode": mode})
        for _ in range(5):
            ad.record_close("doge", "5m", "down", mfe_pct=0.0, pnl=-6.0)
        assert ad.size_multiplier("doge", "5m", "down") == 1.0
        # but the raw multiplier still computes (for shadow logging)
        assert ad.raw_multiplier("doge", "5m", "down") < 1.0


def test_never_green_net_loser_is_desized():
    ad = LaneTapeAdapter(LIVE)
    for _ in range(4):
        ad.record_close("doge", "5m", "down", mfe_pct=0.0, pnl=-6.0)  # never green, deep losses
    m = ad.size_multiplier("doge", "5m", "down")
    assert m <= 0.4, f"expected heavy de-size, got {m}"


def test_choppy_but_net_winner_is_protected():
    ad = LaneTapeAdapter(LIVE)
    # a TP lane: interspersed losers but big greens -> net positive, high green-rate
    seq = [(0.30, 12.0), (0.0, -4.0), (0.25, 9.0), (0.0, -3.0), (0.40, 15.0)]
    for mfe, pnl in seq:
        ad.record_close("bitcoin", "15m", "down", mfe_pct=mfe, pnl=pnl)
    m = ad.size_multiplier("bitcoin", "15m", "down")
    assert m >= 0.99, f"net-winner must not be de-sized, got {m}"


def test_net_loser_that_still_goes_green_is_spared():
    ad = LaneTapeAdapter(LIVE)
    # slightly net-negative but fills DO reach green often -> variance, not tape-turn
    seq = [(0.30, 8.0), (0.0, -5.0), (0.20, 6.0), (0.0, -6.0), (0.0, -6.0)]
    for mfe, pnl in seq:
        ad.record_close("eth", "1h", "up", mfe_pct=mfe, pnl=pnl)
    # green_rate ~ 0.4 (>0 relief); de-size should be partial, not floor
    m = ad.size_multiplier("eth", "1h", "up")
    assert 0.4 < m < 1.0, f"expected partial de-size, got {m}"


def test_symmetric_recovery():
    ad = LaneTapeAdapter(LIVE)
    for _ in range(5):
        ad.record_close("xrp", "5m", "down", mfe_pct=0.0, pnl=-6.0)
    assert ad.size_multiplier("xrp", "5m", "down") < 0.5  # de-sized
    # tape turns back: fills go green and win -> multiplier climbs back to full
    for _ in range(5):
        ad.record_close("xrp", "5m", "down", mfe_pct=0.30, pnl=8.0)
    assert ad.size_multiplier("xrp", "5m", "down") == 1.0  # fully recovered


ADM = {**LIVE, "admission_mode": "live", "admission_loosen_max": 0.03,
       "admission_tighten_max": 0.05, "admission_win_ref_dollars": 4.0}


def test_admission_off_is_zero():
    ad = LaneTapeAdapter({**ADM, "admission_mode": "off"})
    for _ in range(5):
        ad.record_close("doge", "5m", "down", mfe_pct=0.0, pnl=-6.0)
    assert ad.admission_delta("doge", "5m", "down") == 0.0
    assert ad.raw_admission_delta("doge", "5m", "down") > 0.0  # still computes


def test_admission_tightens_losing_never_green():
    ad = LaneTapeAdapter(ADM)
    for _ in range(4):
        ad.record_close("doge", "5m", "down", mfe_pct=0.0, pnl=-6.0)
    d = ad.admission_delta("doge", "5m", "down")
    assert d > 0.02, f"expected tighten (positive), got {d}"


def test_admission_loosens_winner():
    ad = LaneTapeAdapter(ADM)
    for _ in range(4):
        ad.record_close("xrp", "15m", "down", mfe_pct=0.30, pnl=8.0)
    d = ad.admission_delta("xrp", "15m", "down")
    assert d < -0.01, f"expected loosen (negative), got {d}"


def test_admission_bounded():
    ad = LaneTapeAdapter(ADM)
    for _ in range(5):
        ad.record_close("doge", "5m", "down", mfe_pct=0.0, pnl=-99.0)
    assert ad.admission_delta("doge", "5m", "down") <= 0.05 + 1e-9
    ad2 = LaneTapeAdapter(ADM)
    for _ in range(5):
        ad2.record_close("xrp", "15m", "down", mfe_pct=0.9, pnl=99.0)
    assert ad2.admission_delta("xrp", "15m", "down") >= -0.03 - 1e-9


def test_admission_self_corrects():
    # a loosened winner that starts losing flips to tighten
    ad = LaneTapeAdapter(ADM)
    for _ in range(5):
        ad.record_close("xrp", "15m", "down", mfe_pct=0.30, pnl=8.0)
    assert ad.admission_delta("xrp", "15m", "down") < 0  # loosened
    for _ in range(5):
        ad.record_close("xrp", "15m", "down", mfe_pct=0.0, pnl=-6.0)
    assert ad.admission_delta("xrp", "15m", "down") > 0  # now tightened


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
