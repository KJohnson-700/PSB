from __future__ import annotations

from tools.ghost_gate_report import (
    aggregate_gates,
    aggregate_lanes,
    aggregate_probes,
    aggregate_regime_gates,
    aggregate_regimes,
    build_probe_relax_recommendations,
    build_report,
    enrich_rows_from_regime_log,
)

from tests.test_ghost_calibration import _write_jsonl


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
    assert 0.0 <= lane_rows[0]["win_rate_ci_low"] <= lane_rows[0]["win_rate_ci_high"] <= 1.0

    assert len(gate_rows) == 1
    assert gate_rows[0]["reason"] == "eth_15m_weak_confirm"
    assert gate_rows[0]["net_gate_value_pct"] == 0.2
    assert 0.0 <= gate_rows[0]["win_rate_ci_low"] <= gate_rows[0]["win_rate_ci_high"] <= 1.0


def test_ghost_gate_report_aggregates_regime_buckets() -> None:
    rows = [
        {
            "lane_id": "bitcoin|15m|up|neutral|rejected",
            "strategy": "bitcoin",
            "window": "15m",
            "action": "BUY_YES",
            "reason": "lane_min_edge",
            "win": True,
            "realized_pct": 0.5,
            "price_regime": "flat",
            "polymarket_regime": "deadzone",
            "combined_regime": "deadzone_confirmed",
            "btc_1h_regime": "BEAR",
            "convergence_score": 0.41,
        },
        {
            "lane_id": "bitcoin|15m|up|neutral|rejected",
            "strategy": "bitcoin",
            "window": "15m",
            "action": "BUY_YES",
            "reason": "lane_min_edge",
            "win": False,
            "realized_pct": -1.0,
            "price_regime": "flat",
            "polymarket_regime": "deadzone",
            "combined_regime": "deadzone_confirmed",
            "btc_1h_regime": "BEAR",
            "convergence_score": 0.44,
        },
    ]

    regime_rows = aggregate_regimes(rows)
    regime_gate_rows = aggregate_regime_gates(rows)
    report = build_report(rows)

    assert regime_rows[0]["regime_key"] == "flat|deadzone|deadzone_confirmed"
    assert regime_rows[0]["n"] == 2
    assert regime_gate_rows[0]["regime_gate_key"] == (
        "deadzone_confirmed|bitcoin|15m|BUY_YES|lane_min_edge"
    )
    assert report["btc_regimes"][0]["btc_1h_regime"] == "BEAR"
    assert report["convergence"][0]["convergence_bucket"] == "low"
    assert report["deadzone_gates"][0]["combined_regime"] == "deadzone_confirmed"


def test_ghost_gate_report_can_enrich_rows_from_regime_log(tmp_path) -> None:
    regime = tmp_path / "market_regime.jsonl"
    _write_jsonl(
        regime,
        [
            {
                "ts": "2026-05-16T14:01:00+00:00",
                "price_regime": "flat",
                "polymarket_regime": "deadzone",
                "combined_regime": "deadzone_confirmed",
            }
        ],
    )
    rows = [
        {
            "ts": "2026-05-16T14:00:30+00:00",
            "strategy": "bitcoin",
            "window": "15m",
            "action": "BUY_YES",
            "reason": "lane_min_edge",
            "win": True,
            "realized_pct": 0.5,
        }
    ]

    enriched = enrich_rows_from_regime_log(rows, regime_path=regime, max_age_sec=120)

    assert enriched[0]["combined_regime"] == "deadzone_confirmed"
    assert enriched[0]["regime_source"] == "market_regime"


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


def test_ghost_gate_report_actionable_overtight_filters_small_or_uncertain_buckets() -> None:
    rows = []
    rows.extend(
        {
            "lane_id": "bitcoin|1h|down|bearish|rejected",
            "strategy": "bitcoin",
            "window": "1h",
            "action": "BUY_NO",
            "reason": "hist_gate_1h_short_reject",
            "win": i < 80,
            "realized_pct": 0.5 if i < 80 else -1.0,
        }
        for i in range(100)
    )
    rows.extend(
        {
            "lane_id": "bitcoin|5m|down|neutral|rejected",
            "strategy": "bitcoin",
            "window": "5m",
            "action": "BUY_NO",
            "reason": "hist_gate_5m_short_reject",
            "win": True,
            "realized_pct": 0.5,
        }
        for _ in range(20)
    )
    rows.extend(
        {
            "lane_id": "xrp_macro|1h|down|bearish|rejected",
            "strategy": "xrp_macro",
            "window": "1h",
            "action": "BUY_NO",
            "reason": "lane_entry_window",
            "win": i < 40,
            "realized_pct": 0.5 if i < 40 else -1.0,
        }
        for i in range(100)
    )

    report = build_report(rows)
    actionable = report["actionable_overtight_gates"]

    assert len(actionable) == 1
    assert actionable[0]["gate_key"] == "bitcoin|1h|BUY_NO|hist_gate_1h_short_reject"


def test_ghost_gate_report_probe_relaxations_choose_smallest_supported_delta() -> None:
    rows = []
    for i in range(120):
        rows.append(
            {
                "lane_id": "bitcoin|15m|down|bearish|rejected",
                "strategy": "bitcoin",
                "window": "15m",
                "action": "BUY_NO",
                "reason": "hist_gate_15m_short_reject",
                "win": i < 90,
                "realized_pct": 0.5 if i < 90 else -1.0,
                "probe_variants": [
                    {
                        "probe": "hist_support_count",
                        "kind": "relax",
                        "delta": 1.0,
                        "would_pass": i < 90,
                    },
                    {
                        "probe": "hist_support_count",
                        "kind": "relax",
                        "delta": 2.0,
                        "would_pass": True,
                    },
                ],
            }
        )

    probe_rows = aggregate_probes(rows)
    recommendations = build_probe_relax_recommendations(probe_rows)

    assert len(recommendations) == 1
    assert recommendations[0]["strategy"] == "bitcoin"
    assert recommendations[0]["reason"] == "hist_gate_15m_short_reject"
    assert recommendations[0]["probe"] == "hist_support_count"
    assert recommendations[0]["recommended_delta"] == 2.0

    report = build_report(rows)
    assert len(report["actionable_probe_relaxations"]) == 1
    assert (
        report["actionable_probe_relaxations"][0]["recommended_action"]
        == "relax hist_support_count by 2.000000"
    )
