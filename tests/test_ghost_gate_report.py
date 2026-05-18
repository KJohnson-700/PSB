from __future__ import annotations

from src.analysis import ghost_calibration as gc
from tools.ghost_gate_report import aggregate_gates, aggregate_lanes, aggregate_probes


def test_ghost_gate_report_aggregates_lane_and_gate_value() -> None:
    rows = [
        {
            "lane_id": "eth_macro|15m|down|bearish|rejected",
            "strategy": "eth_macro",
            "window": "15m",
            "action": "BUY_NO",
            "reason": "eth_15m_weak_confirm",
            "win": True,
            "realized_pct": 0.8,
        },
        {
            "lane_id": "eth_macro|15m|down|bearish|rejected",
            "strategy": "eth_macro",
            "window": "15m",
            "action": "BUY_NO",
            "reason": "eth_15m_weak_confirm",
            "win": False,
            "realized_pct": -1.0,
        },
    ]

    lane_rows = aggregate_lanes(rows)
    gate_rows = aggregate_gates(rows)

    assert len(lane_rows) == 1
    assert lane_rows[0]["n"] == 2
    assert lane_rows[0]["missed_ev_pct"] == 0.8
    assert lane_rows[0]["protected_loss_pct"] == 1.0
    assert lane_rows[0]["net_gate_value_pct"] == 0.2

    assert len(gate_rows) == 1
    assert gate_rows[0]["reason"] == "eth_15m_weak_confirm"
    assert gate_rows[0]["net_gate_value_pct"] == 0.2


def test_ghost_gate_report_probe_variants_only_count_would_pass_rows() -> None:
    rows = [
        {
            "lane_id": "bitcoin|15m|down|bearish|rejected",
            "strategy": "bitcoin",
            "window": "15m",
            "action": "BUY_NO",
            "reason": "lane_min_edge",
            "win": True,
            "realized_pct": 0.5,
            "probe_variants": [
                {
                    "probe": "min_edge",
                    "kind": "baseline",
                    "delta": 0.0,
                    "would_pass": False,
                },
                {
                    "probe": "min_edge",
                    "kind": "relax",
                    "delta": 0.01,
                    "would_pass": True,
                },
            ],
        }
    ]

    probe_rows = aggregate_probes(rows)
    assert len(probe_rows) == 1
    assert probe_rows[0]["probe"] == "min_edge"
    assert probe_rows[0]["kind"] == "relax"
    assert probe_rows[0]["delta"] == 0.01
    assert probe_rows[0]["avg_realized_pct"] == 0.5
