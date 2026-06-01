import pytest
import yaml
from pathlib import Path

from src.analysis.buy_yes_lane_repair import resolve_buy_yes_lane_repair


def test_buy_yes_lane_repair_applies_probability_and_edge_soft_penalty():
    cfg = {
        "buy_yes_lane_repair": {
            "enabled": True,
            "rules": [
                {
                    "name": "xrp_5m_native_overconfidence",
                    "window": "5m",
                    "entry_family": "xrp_5m_native",
                    "probability_haircut": 0.06,
                    "min_edge_add": 0.03,
                }
            ],
        }
    }

    result = resolve_buy_yes_lane_repair(
        strategy_config=cfg,
        strategy="xrp_macro",
        window_size="5m",
        action="BUY_YES",
        lane_side="up",
        entry_family="xrp_5m_native",
        estimated_prob=0.56,
        yes_price=0.49,
        edge=0.07,
        effective_min_edge=0.06,
    )

    assert result.matched is True
    assert result.estimated_prob == pytest.approx(0.50)
    assert result.edge == pytest.approx(0.01)
    assert result.effective_min_edge == pytest.approx(0.09)
    assert "prob_haircut=0.060" in result.reason_token


def test_buy_yes_lane_repair_can_add_basis_penalty_without_disabling_lane():
    cfg = {
        "buy_yes_lane_repair": {
            "enabled": True,
            "rules": [
                {
                    "entry_family": "eth_15m_native",
                    "probability_haircut": 0.04,
                    "min_edge_add": 0.015,
                    "oracle_basis_abs_bps_min": 15.0,
                    "oracle_basis_min_edge_add": 0.01,
                }
            ],
        }
    }

    result = resolve_buy_yes_lane_repair(
        strategy_config=cfg,
        strategy="eth_macro",
        window_size="15m",
        action="BUY_YES",
        lane_side="up",
        entry_family="eth_15m_native",
        estimated_prob=0.61,
        yes_price=0.48,
        edge=0.13,
        effective_min_edge=0.11,
        oracle_basis_bps=18.0,
    )

    assert result.matched is True
    assert result.edge == pytest.approx(0.09)
    assert result.effective_min_edge == pytest.approx(0.135)
    assert result.oracle_basis_min_edge_add == pytest.approx(0.01)


def test_buy_yes_lane_repair_ignores_buy_no_and_nonmatching_family():
    cfg = {
        "buy_yes_lane_repair": {
            "enabled": True,
            "rules": [{"entry_family": "hype_15m_spike", "probability_haircut": 0.06}],
        }
    }

    buy_no = resolve_buy_yes_lane_repair(
        strategy_config=cfg,
        strategy="hype_macro",
        window_size="15m",
        action="BUY_NO",
        lane_side="down",
        entry_family="hype_15m_spike",
        estimated_prob=0.40,
        yes_price=0.52,
        edge=0.12,
        effective_min_edge=0.08,
    )
    other = resolve_buy_yes_lane_repair(
        strategy_config=cfg,
        strategy="hype_macro",
        window_size="15m",
        action="BUY_YES",
        lane_side="up",
        entry_family="hype_15m_native",
        estimated_prob=0.60,
        yes_price=0.52,
        edge=0.08,
        effective_min_edge=0.06,
    )

    assert buy_no.matched is False
    assert other.matched is False


def test_configured_buy_yes_repairs_are_soft_and_target_only_bad_lanes():
    cfg = yaml.safe_load(Path("config/settings.yaml").read_text())
    strategies = cfg["strategies"]
    assert "buy_yes_wr_mode" not in strategies["bitcoin"]
    expected = {
        "eth_macro": {
            "eth_5m_native",
            "eth_15m_native",
            "eth_1h_native",
            "drift",
        },
        "hype_macro": {"spike"},
        "xrp_macro": {
            "xrp_5m_native",
            "xrp_15m_native",
            "xrp_5m_neutral_fallback_1h",
        },
    }
    for strategy, families in expected.items():
        repair = strategies[strategy]["buy_yes_lane_repair"]
        assert repair["enabled"] is True
        configured = {rule["entry_family"] for rule in repair["rules"]}
        assert configured == families
        for rule in repair["rules"]:
            assert "enabled" not in rule
            assert "allowed_families" not in rule
            assert "disable" not in " ".join(rule)
            assert float(rule.get("probability_haircut", 0.0)) >= 0.0
            assert float(rule.get("min_edge_add", 0.0)) >= 0.0
