import logging

from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.bitcoin import BitcoinStrategy
from src.strategies.sol_macro import SolMacroStrategy
from src.strategies.weather import WeatherStrategy


class _StubAIAgent(AIAgent):
    def __init__(self):
        pass


def _base_config() -> dict:
    return {
        "trading": {
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
            "default_position_size": 10,
            "max_position_size": 15,
        },
        "exposure": {},
        "strategies": {},
    }


def test_bitcoin_missing_enabled_fails_closed_and_warns(caplog):
    cfg = _base_config()
    cfg["strategies"]["bitcoin"] = {"min_edge": 0.10}
    with caplog.at_level(logging.WARNING):
        strat = BitcoinStrategy(cfg, _StubAIAgent(), PositionSizer())
    assert strat.enabled is False
    assert "missing required config key 'enabled'" in caplog.text


def test_sol_macro_missing_enabled_fails_closed_and_warns(caplog):
    cfg = _base_config()
    cfg["strategies"]["sol_macro"] = {"min_edge": 0.10}
    with caplog.at_level(logging.WARNING):
        strat = SolMacroStrategy(cfg, _StubAIAgent(), PositionSizer())
    assert strat.enabled is False
    assert "missing required config key 'enabled'" in caplog.text


def test_weather_explicit_enabled_still_respected():
    cfg = _base_config()
    cfg["strategies"]["weather"] = {"enabled": True}
    strat = WeatherStrategy(cfg, PositionSizer())
    assert strat.enabled is True
