"""Tests for OPS_JSON snapshot helpers."""

from types import SimpleNamespace

from src.ops_pulse import _decision_gate_digest, _scan_skip_digest, build_ops_snapshot


def test_scan_skip_digest_aggregate():
    ai = {
        "sol_macro": {"top_skip_reasons": {"outside_entry_window": 16, "min_edge": 2}},
        "eth_macro": {"top_skip_reasons": {"outside_entry_window": 10, "oracle": 1}},
    }
    d = _scan_skip_digest(ai)
    assert d["per_strategy"]["sol_macro"]["outside_entry_window"] == 16
    assert d["aggregate_top"]["outside_entry_window"] == 26


def test_decision_gate_digest_surfaces_oracle_composite_and_enforced_lanes():
    cfg = {
        "updown_composite": {
            "default_min_score": 0.62,
            "btc_neutral_15m_min_score": 0.68,
            "hype_15m_buy_yes_min_score": 0.70,
        },
        "ai": {
            "decision_layer": {
                "enabled": True,
                "min_confidence": 0.60,
                "hard_skip_if_unavailable_on_enforced": True,
                "use_shadow_portfolio": False,
                "enforced_lanes": {
                    "bitcoin": ["neutral_15m", "marginal"],
                    "hype_macro": ["marginal", "15m_buy_yes"],
                },
            }
        },
        "strategies": {
            "bitcoin": {
                "neutral_15m_requires_shadow_portfolio": True,
                "neutral_15m_min_composite_score": 0.68,
            },
            "hype_macro": {
                "require_oracle_for_updown": True,
                "oracle_max_age_sec": 180,
                "oracle_max_basis_bps": 12.0,
                "require_shadow_portfolio_15m_buy_yes": True,
                "calibration_size_multiplier_15m_buy_yes": 0.35,
            },
        },
    }
    stats = {
        "bitcoin": {
            "top_skip_reasons": {"composite_score_below_floor": 2, "ai_decision_direct_ai_hold": 1},
            "gate_distributions": {"composite_score": {"min": 0.52, "avg": 0.63, "max": 0.71}},
        },
        "hype_macro": {
            "top_skip_reasons": {"oracle_basis_block": 1},
            "gate_distributions": {"composite_score": {"avg": 0.69}},
        },
    }

    digest = _decision_gate_digest(cfg, stats)

    assert digest["enabled"] is True
    assert digest["floors"]["hype_15m_buy_yes_min_score"] == 0.70
    assert digest["lanes"]["bitcoin"]["enforced_lanes"] == ["neutral_15m", "marginal"]
    assert digest["lanes"]["bitcoin"]["shadow_required"] is True
    assert digest["lanes"]["hype_macro"]["oracle"]["max_basis_bps"] == 12.0
    assert digest["lanes"]["hype_macro"]["size_multiplier_15m_buy_yes"] == 0.35
    assert digest["active_blocks"]["hype_macro"]["oracle_basis_block"] == 1


def test_build_ops_snapshot_includes_skip_digest_and_regime():
    class J:
        session_id = "s1"
        session_dir = "/tmp"
        _entries_file = "/tmp/e.jsonl"

        def get_summary(self):
            return {
                "open_positions": 0,
                "total_exits": 0,
                "total_entries": 0,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "total_pnl": 0,
            }

    class Rm:
        daily_trades = 0
        daily_pnl = 0.0

    class Em:
        loss_kill_switch_enabled = True
        max_consecutive_losses = 3

    bot = SimpleNamespace(
        config={
            "trading": {
                "dry_run": True,
                "regime": {
                    "enabled": True,
                    "btc_break_above_usd": 80000,
                    "btc_break_below_usd": 75000,
                },
            },
            "ai": {
                "enabled": True,
                "live_inferencing": True,
                "provider_chain": [
                    {
                        "name": "minimax",
                        "type": "minimax",
                        "api_key_secret": "MINIMAX_API_KEY",
                    }
                ],
            },
            "logging": {"ops_pulse": False},
        },
        journal=J(),
        risk_manager=Rm(),
        btc_exposure_manager=Em(),
        bankroll=1000.0,
        running=True,
        last_signal_counts={},
        cumulative_signal_counts={},
        last_cycle_times={},
        ai_agent=SimpleNamespace(api_keys={"MINIMAX_API_KEY": "sk-test"}),
        last_ai_scan_stats={
            "bitcoin": {
                "allowed_side": "LONG",
                "btc_spot_usd": 78500.0,
                "top_skip_reasons": {"x": 1},
            },
            "sol_macro": {"top_skip_reasons": {"outside_entry_window": 5}},
        },
        scan_interval=60,
    )

    def _ks():
        return False

    bot._kill_switch_active = _ks
    snap = build_ops_snapshot(bot, "test")
    assert "scan_skip_digest" in snap
    assert snap["scan_skip_digest"]["aggregate_top"].get("outside_entry_window") == 5
    assert "ai_pipeline" in snap
    assert snap["ai_pipeline"]["aggregate"] == {}
    assert "decision_gates" in snap
    assert snap["decision_gates"]["enabled"] is False
    assert snap["ai_status"]["ready"] is True
    assert "zero calls" in snap["ai_activity_note"]
    assert snap["side_selection"]["aggregate"]["LONG"] == 1
    assert snap["side_selection"]["short_lanes"] == []
    assert "No strategy selected SHORT" in snap["side_selection"]["buy_no_absence_reason"]
    assert snap["timestamps_policy"]["canonical"] == "UTC"
    assert snap["regime"]["btc_spot_usd"] == 78500.0
    assert snap["regime"].get("spot_gte_break_high") is False


def test_build_ops_snapshot_ai_pipeline_digest_aggregates_aliases():
    class J:
        session_id = "s1"
        session_dir = "/tmp"
        _entries_file = "/tmp/e.jsonl"

        def get_summary(self):
            return {
                "open_positions": 0,
                "total_exits": 0,
                "total_entries": 0,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "total_pnl": 0,
            }

    bot = SimpleNamespace(
        config={"trading": {"dry_run": True}, "logging": {"ops_pulse": False}},
        journal=J(),
        risk_manager=SimpleNamespace(daily_trades=0, daily_pnl=0.0),
        btc_exposure_manager=SimpleNamespace(
            loss_kill_switch_enabled=True,
            max_consecutive_losses=3,
        ),
        bankroll=1000.0,
        running=True,
        last_signal_counts={},
        cumulative_signal_counts={},
        last_cycle_times={},
        ai_agent=SimpleNamespace(api_keys={}),
        last_ai_scan_stats={
            "eth_macro": {
                "ai_calls": 4,
                "research_calls": 2,
                "research_plans_logged": 1,
                "shadow_pipeline_calls": 3,
                "shadow_pipeline_ok": 2,
                "shadow_marginal_mismatch": 1,
            },
            "bitcoin": {"ai_pipeline_calls": 3, "ai_assists": 2, "ai_overrides": 1},
        },
        scan_interval=60,
    )
    bot._kill_switch_active = lambda: False

    snap = build_ops_snapshot(bot, "test")
    per = snap["ai_pipeline"]["per_strategy"]
    agg = snap["ai_pipeline"]["aggregate"]
    assert per["eth_macro"]["ai_pipeline_calls"] == 4
    assert per["eth_macro"]["research_calls"] == 2
    assert per["eth_macro"]["ai_assists"] == 1
    assert per["bitcoin"]["ai_pipeline_calls"] == 3
    assert per["bitcoin"]["ai_assists"] == 2
    assert per["bitcoin"]["ai_overrides"] == 1
    assert agg["ai_pipeline_calls"] == 7
    assert agg["ai_assists"] == 3
    assert agg["ai_overrides"] == 1
    assert agg["research_calls"] == 2
    assert agg["shadow_pipeline_calls"] == 3
    assert agg["shadow_pipeline_ok"] == 2
    assert agg["shadow_marginal_mismatch"] == 1


def test_build_ops_snapshot_side_selection_surfaces_short_side():
    class J:
        session_id = "s1"
        session_dir = "/tmp"

        def get_summary(self):
            return {
                "open_positions": 0,
                "total_exits": 0,
                "total_entries": 0,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "total_pnl": 0,
            }

    bot = SimpleNamespace(
        config={"trading": {"dry_run": True}, "logging": {"ops_pulse": False}},
        journal=J(),
        risk_manager=SimpleNamespace(daily_trades=0, daily_pnl=0.0),
        btc_exposure_manager=SimpleNamespace(
            loss_kill_switch_enabled=True,
            max_consecutive_losses=3,
        ),
        bankroll=1000.0,
        running=True,
        last_signal_counts={},
        cumulative_signal_counts={},
        last_cycle_times={},
        ai_agent=SimpleNamespace(api_keys={}),
        last_ai_scan_stats={
            "bitcoin": {"allowed_side": "LONG", "signals": 0},
            "eth_macro": {
                "allowed_side": "SHORT",
                "signals": 1,
                "side_source_counts": {"hybrid_strong_short": 1},
            },
        },
        scan_interval=60,
    )
    bot._kill_switch_active = lambda: False

    snap = build_ops_snapshot(bot, "test")
    assert snap["side_selection"]["aggregate"]["SHORT"] == 1
    assert snap["side_selection"]["short_lanes"] == ["eth_macro"]
    assert snap["side_selection"]["buy_no_absence_reason"] == ""
