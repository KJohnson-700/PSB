from types import SimpleNamespace
from unittest.mock import Mock

from src.main import PolyBot


def test_scan_diagnostics_annotation_persists_silent_lane_reasons() -> None:
    journal = Mock()
    bot = SimpleNamespace(
        journal=journal,
        _unified_cycle_count=42,
        last_signal_counts={"sol_macro": 0, "eth_macro": 1},
        cumulative_signal_counts={"sol_macro": 0, "eth_macro": 5},
        last_ai_scan_stats={
            "sol_macro": {
                "enabled": True,
                "signals": 0,
                "markets_considered": 11,
                "allowed_side": "LONG",
                "action_counts": {"BUY_YES": 1},
                "side_source_counts": {"bullish_rally_default": 1},
                "top_skip_reasons": {"lane_entry_window": 9},
            },
            "eth_macro": {
                "enabled": True,
                "signals": 1,
                "markets_considered": 11,
                "allowed_side": "LONG",
                "action_counts": {"BUY_YES": 1},
                "top_skip_reasons": {},
            },
        },
    )

    PolyBot._append_scan_diagnostics_annotation(
        bot,
        scan_skip_digest={"per_strategy": {"sol_macro": {"lane_entry_window": 9}}},
        side_selection={"long_lanes": ["sol_macro", "eth_macro"]},
    )

    journal.append_annotation.assert_called_once()
    kwargs = journal.append_annotation.call_args.kwargs
    assert kwargs["trade_id"] == "__scan_diagnostics__::42"
    assert kwargs["strategy"] == "scan_diagnostics"
    assert kwargs["extra"]["source"] == "scan_diagnostics"
    assert kwargs["extra"]["per_strategy"]["sol_macro"]["top_skip_reasons"] == {
        "lane_entry_window": 9
    }
