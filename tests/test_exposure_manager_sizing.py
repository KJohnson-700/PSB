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


def test_explicit_tier_floors_override_scaled_legacy_floor() -> None:
    mgr = ExposureManager(
        {
            "exposure": {
                "full_size": 25.0,
                "moderate_size": 15.0,
                "minimal_size": 8.0,
                "min_trade_usd": 10.0,
                "full_min_trade_usd": 10.0,
                "moderate_min_trade_usd": 8.0,
                "minimal_min_trade_usd": 5.0,
            }
        }
    )

    mgr._current_tier = ExposureTier.MODERATE
    assert mgr.scale_size(10.0) == 8.0

    mgr._current_tier = ExposureTier.MINIMAL
    assert mgr.scale_size(10.0) == 5.0


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


def test_loss_pause_auto_resumes_by_default_in_paper() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "max_pause_cycles": 1,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="TEST")
    mgr.record_trade(-1.0, strategy="bitcoin")
    ok = MarketConditions(volatility=0.02, volume_ratio=1.2, trend_strength=0.8)

    tier, *_ = mgr.get_exposure(ok)
    assert tier != ExposureTier.PAUSED


def test_loss_kill_trigger_records_latest_lane_context() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "max_consecutive_losses": 1,
            "pause_cycles": 2,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="DOGE")
    mgr.record_trade(-1.0, strategy="doge_macro", market_id="m1", window_size="5m")

    status = mgr.get_status()
    trigger = status["last_loss_kill_trigger"]
    assert status["paused"] is True
    assert trigger is not None
    assert trigger["lane"] == "DOGE"
    assert trigger["window_size"] == "5m"
    assert "consecutive losses" in trigger["reason"]


def test_pause_resume_requires_recovery_and_green_non_deadzone_window() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "max_pause_cycles": 5,
            "loss_pause_recovery_multiple": 2.0,
            "require_green_window_for_resume": True,
            "require_non_deadzone_for_resume": True,
            "live_resume_mode": "auto",
            "low_volume_ratio": 0.7,
            "low_vol_pct": 0.005,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="SOL")
    mgr.update_portfolio_pnl(-10.0)
    mgr.record_trade(-3.0, strategy="sol_macro", window_size="5m")

    ok = MarketConditions(volatility=0.02, volume_ratio=1.2, trend_strength=0.8)

    mgr.update_resume_window(green_window=False, in_deadzone=False)
    t1, *_ = mgr.get_exposure(ok)
    assert t1 == ExposureTier.PAUSED

    mgr.update_resume_window(green_window=True, in_deadzone=True)
    t2, *_ = mgr.get_exposure(ok)
    assert t2 == ExposureTier.PAUSED

    mgr.update_resume_window(green_window=True, in_deadzone=False)
    mgr.update_portfolio_pnl(-5.0)  # recovered 5 < target 6
    t3, *_ = mgr.get_exposure(ok)
    assert t3 == ExposureTier.PAUSED

    mgr.update_portfolio_pnl(-4.0)  # recovered 6 == target
    t4, *_ = mgr.get_exposure(ok)
    assert t4 != ExposureTier.PAUSED


def test_pause_recovery_anchor_uses_post_loss_portfolio_pnl() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "loss_pause_recovery_multiple": 2.0,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="BTC")
    mgr.update_portfolio_pnl(-13.0)
    mgr.record_trade(-3.0, strategy="bitcoin", window_size="5m")

    status = mgr.get_status()
    assert status["pause_recovery_anchor_pnl"] == -13.0
    assert status["pause_recovery_target"] == 6.0
