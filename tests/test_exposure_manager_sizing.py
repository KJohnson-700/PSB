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


def test_loss_streak_pause_is_live_only_inert_in_paper() -> None:
    """The loss-streak lane pause is a live-trading safety. A paper session must
    keep trading a losing lane (calibration), not pause it — even with the switch
    enabled. Live sessions still pause."""
    cfg = {"exposure": {"loss_kill_switch_enabled": True, "max_consecutive_losses": 3}}

    paper = ExposureManager(cfg, is_paper=True, lane_name="HYPE")
    assert paper.loss_kill_active is False
    for _ in range(4):
        paper.record_trade(pnl=-1.0)
    assert paper.get_status()["paused"] is False

    live = ExposureManager(cfg, is_paper=False, lane_name="HYPE")
    assert live.loss_kill_active is True
    for _ in range(3):
        live.record_trade(pnl=-1.0)
    assert live.get_status()["paused"] is True


def test_partial_reload_does_not_reenable_loss_kill_switch() -> None:
    """A partial (key-missing) reload must preserve an explicitly-disabled kill
    switch — regression for the config `false` silently flipping to `true` and
    lighting the dashboard LOSS KILL badge after a sizing-only config update."""
    mgr = ExposureManager(
        {"exposure": {"loss_kill_switch_enabled": False}},
        is_paper=True,
        lane_name="TEST",
    )
    assert mgr.loss_kill_switch_enabled is False
    # sizing-only update, no loss_kill key present
    mgr.reload_from_config({"full_size": 40.0})
    assert mgr.loss_kill_switch_enabled is False
    # explicit values still take effect
    mgr.reload_from_config({"loss_kill_switch_enabled": True})
    assert mgr.loss_kill_switch_enabled is True
    mgr.reload_from_config({"loss_kill_switch_enabled": False})
    assert mgr.loss_kill_switch_enabled is False


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
            "loss_kill_apply_in_paper": True,  # these tests exercise the paper pause
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "max_pause_cycles": 2,
            "live_resume_mode": "auto",
            "low_volume_ratio": 0.7,
            "low_vol_pct": 0.005,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="TEST")
    # Per-lane kill switch (2026-07-25): pause/resume now via lane_paused(window, side).
    # record_trade with no window → lane key "|"; drive resume on that same lane.
    mgr.record_trade(-1.0, strategy="bitcoin")
    chop = MarketConditions(volatility=0.001, volume_ratio=0.1, trend_strength=0.0)

    first_paused, _ = mgr.lane_paused("", "", chop)
    second_paused, _ = mgr.lane_paused("", "", chop)

    assert first_paused is True
    assert second_paused is False  # force-resumed after max_pause_cycles


def test_loss_pause_auto_resumes_by_default_in_paper() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "loss_kill_apply_in_paper": True,  # these tests exercise the paper pause
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "max_pause_cycles": 1,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="TEST")
    mgr.record_trade(-1.0, strategy="bitcoin")
    ok = MarketConditions(volatility=0.02, volume_ratio=1.2, trend_strength=0.8)

    # Per-lane kill switch: good conditions auto-resume the paused lane ("|") by default.
    paused, _ = mgr.lane_paused("", "", ok)
    assert paused is False


def test_loss_kill_trigger_records_latest_lane_context() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "loss_kill_apply_in_paper": True,  # these tests exercise the paper pause
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


def test_pause_resume_requires_recovery_and_green_window() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "loss_kill_apply_in_paper": True,  # these tests exercise the paper pause
            "max_consecutive_losses": 1,
            "pause_cycles": 1,
            "max_pause_cycles": 5,
            "loss_pause_recovery_multiple": 2.0,
            "require_green_window_for_resume": True,
            "live_resume_mode": "auto",
            "low_volume_ratio": 0.7,
            "low_vol_pct": 0.005,
        }
    }
    mgr = ExposureManager(cfg, is_paper=True, lane_name="SOL")
    mgr.update_portfolio_pnl(-10.0)
    mgr.record_trade(-3.0, strategy="sol_macro", window_size="5m")

    ok = MarketConditions(volatility=0.02, volume_ratio=1.2, trend_strength=0.8)

    # Per-lane kill switch: resume gating (green window + recovery) now on lane "5m|".
    mgr.update_resume_window(green_window=False)
    p1, _ = mgr.lane_paused("5m", "", ok)
    assert p1 is True

    mgr.update_resume_window(green_window=True)
    mgr.update_portfolio_pnl(-5.0)  # recovered 5 < target 6
    p3, _ = mgr.lane_paused("5m", "", ok)
    assert p3 is True

    mgr.update_portfolio_pnl(-4.0)  # recovered 6 == target
    p4, _ = mgr.lane_paused("5m", "", ok)
    assert p4 is False


def test_pause_recovery_anchor_uses_post_loss_portfolio_pnl() -> None:
    cfg = {
        "exposure": {
            "loss_kill_switch_enabled": True,
            "loss_kill_apply_in_paper": True,  # these tests exercise the paper pause
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
