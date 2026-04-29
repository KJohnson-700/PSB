from src.execution.exposure_manager import ExposureManager, ExposureTier, MarketConditions


def _manager() -> ExposureManager:
    cfg = {
        "exposure": {
            "full_size": 15.0,
            "moderate_size": 13.0,
            "minimal_size": 10.0,
            "min_trade_usd": 10.0,
        }
    }
    return ExposureManager(cfg)


def test_minimal_tier_uses_scaled_floor_not_full_floor() -> None:
    mgr = _manager()
    mgr._current_tier = ExposureTier.MINIMAL
    # raw_size * 0.2 = 1.0, tier floor should be 2.0 (not 10.0)
    assert mgr.scale_size(5.0) == 2.0


def test_moderate_tier_uses_scaled_floor() -> None:
    mgr = _manager()
    mgr._current_tier = ExposureTier.MODERATE
    # raw_size * 0.6 = 1.2, tier floor should be 6.0
    assert mgr.scale_size(2.0) == 6.0


def test_full_tier_floor_unchanged() -> None:
    mgr = _manager()
    mgr._current_tier = ExposureTier.FULL
    # FULL keeps min_trade_usd floor and full-size cap behavior
    assert mgr.scale_size(5.0) == 10.0
    assert mgr.scale_size(30.0) == 15.0


def test_auto_pause_force_resumes_after_max_pause_cycles() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "max_pause_cycles": 2,
            "live_resume_mode": "auto",
            "low_volume_ratio": 0.7,
            "low_vol_pct": 0.005,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="TEST")
    mgr.record_trade(-1.0, strategy="bitcoin")
    chop = MarketConditions(volatility=0.001, volume_ratio=0.1, trend_strength=0.0)

    first_tier, *_ = mgr.get_exposure(chop)
    second_tier, *_ = mgr.get_exposure(chop)

    assert first_tier == ExposureTier.PAUSED
    assert second_tier != ExposureTier.PAUSED
