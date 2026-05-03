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
    assert snap["timestamps_policy"]["canonical"] == "UTC"
    assert snap["regime"]["btc_spot_usd"] == 78500.0
    assert snap["regime"].get("spot_gte_break_high") is False
