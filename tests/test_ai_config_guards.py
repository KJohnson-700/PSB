from pathlib import Path

import yaml


def _settings() -> dict:
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    return yaml.safe_load(cfg_path.read_text())


def test_restart_candidate_enables_real_ai_decision_layer() -> None:
    cfg = _settings()

    assert cfg["ai"]["decision_layer"]["enabled"] is True
    assert cfg["ai"]["decision_layer"]["marginal_veto_only"] is True


def test_btc_ai_policy_is_marginal_15m_1h_not_5m() -> None:
    cfg = _settings()
    btc = cfg["strategies"]["bitcoin"]
    lanes = cfg["ai"]["decision_layer"]["enforced_lanes"]["bitcoin"]

    assert btc["use_ai"] is True
    assert btc["use_ai_updown"] is True
    assert btc["use_ai_updown_5m"] is False
    assert lanes == ["marginal"]


def test_alt_ai_policy_keeps_sorting_gating_lanes_enabled() -> None:
    cfg = _settings()
    enforced = cfg["ai"]["decision_layer"]["enforced_lanes"]

    for strategy in (
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
    ):
        assert cfg["strategies"][strategy]["use_ai"] is True
        assert cfg["strategies"][strategy]["use_ai_updown"] is True
        assert enforced[strategy] == ["default", "marginal"]
