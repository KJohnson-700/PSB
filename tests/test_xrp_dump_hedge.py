"""Tests for XRP dump-and-hedge helpers and strategy (no LLM)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analysis.math_utils import PositionSizer
from tests.async_helpers import run_async
from src.market.scanner import Market
from src.strategies.xrp_dump_hedge import (
    XRPDumpHedgeStrategy,
    dump_triggered,
    hedge_pair_ok,
)


def test_dump_triggered():
    ok, drop = dump_triggered(0.50, 0.40, 0.15)
    assert ok
    assert abs(drop - 0.2) < 1e-6
    ok2, _ = dump_triggered(0.50, 0.44, 0.15)
    assert not ok2


def test_hedge_pair_ok():
    ok, pair = hedge_pair_ok(0.48, 0.46, 0.95)
    assert ok
    assert abs(pair - 0.94) < 1e-9
    ok2, _ = hedge_pair_ok(0.50, 0.50, 0.95)
    assert not ok2


def _mk(yes: float, no: float) -> Market:
    return Market(
        id="m1",
        question="XRP Up or Down 2:15AM–2:30AM ET",
        description="",
        volume=0,
        liquidity=50_000,
        yes_price=yes,
        no_price=no,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=12),
        token_id_yes="y",
        token_id_no="n",
        group_item_title="",
    )


async def _strategy_leg2_after_pending():
    cfg = {
        "trading": {
            "default_position_size": 5,
            "max_position_size": 50,
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
        },
        "strategies": {
            "xrp_dump_hedge": {
                "enabled": True,
                "min_liquidity": 1000,
                "max_spread": 0.08,
                "dump_move_frac": 0.10,
                "max_pair_cost": 0.96,
                "use_btc_z_gate": False,
                "kelly_fraction": 0.10,
            }
        },
    }
    pos = PositionSizer(
        kelly_fraction=0.25,
        max_position_pct=0.05,
        min_position=5,
        max_position=50,
    )
    st = XRPDumpHedgeStrategy(cfg, pos)
    st.enabled = True
    for _ in range(20):
        m = _mk(0.50, 0.50)
        await st.scan_and_analyze([m], 10_000)
    m_peak = _mk(0.50, 0.50)
    await st.scan_and_analyze([m_peak], 10_000)
    m_dump = _mk(0.38, 0.62)
    sigs1 = await st.scan_and_analyze([m_dump], 10_000)
    assert any(s.leg == "1" for s in sigs1)
    m_hedge = _mk(0.48, 0.47)
    sigs2 = await st.scan_and_analyze([m_hedge], 10_000)
    assert any(s.leg == "2" and s.action == "BUY_NO" for s in sigs2)


def test_strategy_leg2_after_pending():
    run_async(_strategy_leg2_after_pending())
