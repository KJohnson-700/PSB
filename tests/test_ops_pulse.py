"""Tests for OPS_JSON snapshot helpers."""

from types import SimpleNamespace

from src.ops_pulse import _scan_skip_digest, build_ops_snapshot


def test_scan_skip_digest_aggregate():
    ai = {
        "sol_macro": {"top_skip_reasons": {"outside_entry_window": 16, "min_edge": 2}},
        "eth_macro": {"top_skip_reasons": {"outside_entry_window": 10, "oracle": 1}},
    }
    d = _scan_skip_digest(ai)
    assert d["per_strategy"]["sol_macro"]["outside_entry_window"] == 16
    assert d["aggregate_top"]["outside_entry_window"] == 26


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
        last_ai_scan_stats={
            "bitcoin": {"btc_spot_usd": 78500.0, "top_skip_reasons": {"x": 1}},
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
