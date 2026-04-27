from src.execution.exposure_manager import ExposureManager, ExposureTier


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
