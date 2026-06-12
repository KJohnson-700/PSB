"""Tests for the NEUTRAL sit-out tape-EV shadow logger + settler.

The logger (`SolMacroStrategy._shadow_log_neutral_sitout`, inherited by ETH and
all alts) records a log-only row with the tape-implied (window-delta) notional
side whenever a lane sits out because its own timeframe had no usable bias. The
settler scores those rows against real Polymarket outcomes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.strategies.eth_macro import ETHMacroStrategy
from tests.test_eth_macro import _config


def _strat():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    return strat


def _market(yes=0.52, no=0.48, mins=10.0):
    return SimpleNamespace(
        end_date=datetime.now(timezone.utc) + timedelta(minutes=mins),
        yes_price=yes,
        no_price=no,
        id="mkt-1",
        slug="eth-updown-15m-1",
    )


def _read_rows(tmp_path):
    p = tmp_path / "data" / "calibration" / "neutral_sitout_shadow.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_sitout_logs_tape_long_when_price_up(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    strat = _strat()
    # price rose since window open -> tape lean up -> BUY_YES
    asset = SimpleNamespace(window_open_15m=100.0, current_price=101.0, atr_14=1.0)
    strat._shadow_log_neutral_sitout(
        asset,
        "15m",
        _market(),
        primary_htf_bias="NEUTRAL",
        alt_trends={"alt_1h_trend": "NEUTRAL", "alt_15m_trend": "NEUTRAL", "alt_5m_trend": "UP"},
    )
    rows = _read_rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "BUY_YES"
    assert r["reason"] == "neutral_sitout"
    assert r["window"] == "15m"
    assert r["wd_prob"] >= 0.5
    assert r["market_id"] == "mkt-1"
    assert r["alt_5m_trend"] == "UP"


def test_sitout_logs_tape_short_when_price_down(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    strat = _strat()
    asset = SimpleNamespace(window_open_15m=100.0, current_price=99.0, atr_14=1.0)
    strat._shadow_log_neutral_sitout(asset, "15m", _market(), primary_htf_bias="NEUTRAL")
    rows = _read_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "BUY_NO"
    assert rows[0]["wd_prob"] < 0.5


def test_sitout_cooldown_dedups_repeated_market(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    strat = _strat()
    strat.config["neutral_sitout_cooldown_sec"] = 300.0
    asset = SimpleNamespace(
        window_open_15m=100.0, window_open_5m=100.0, current_price=101.0, atr_14=1.0
    )
    for _ in range(3):
        strat._shadow_log_neutral_sitout(asset, "15m", _market(), primary_htf_bias="NEUTRAL")
    # Same (market_id, tf) within cooldown -> logged once.
    assert len(_read_rows(tmp_path)) == 1
    # A different timeframe is a distinct cooldown key -> logged.
    strat._shadow_log_neutral_sitout(asset, "5m", _market(), primary_htf_bias="NEUTRAL")
    assert len(_read_rows(tmp_path)) == 2


def test_sitout_disabled_flag_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    strat = _strat()
    strat.config["neutral_sitout_shadow_log"] = False
    asset = SimpleNamespace(window_open_15m=100.0, current_price=101.0, atr_14=1.0)
    strat._shadow_log_neutral_sitout(asset, "15m", _market(), primary_htf_bias="NEUTRAL")
    assert _read_rows(tmp_path) == []


def test_sitout_missing_window_open_no_row_no_raise(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    strat = _strat()
    # No window_open_15m -> evaluate_window_delta returns None -> fail-open, no row.
    asset = SimpleNamespace(window_open_15m=0.0, current_price=101.0, atr_14=1.0)
    strat._shadow_log_neutral_sitout(asset, "15m", _market(), primary_htf_bias="NEUTRAL")
    assert _read_rows(tmp_path) == []


def test_settler_scores_tape_side_against_outcome(monkeypatch, tmp_path):
    import tools.settle_neutral_sitouts as settler

    shadow = tmp_path / "shadow.jsonl"
    settled = tmp_path / "settled.jsonl"
    shadow.write_text(
        json.dumps(
            {
                "ts": 1.0,
                "strategy": "eth_macro",
                "window": "15m",
                "action": "BUY_YES",
                "reason": "neutral_sitout",
                "yes_price": 0.50,
                "no_price": 0.50,
                "wd_prob": 0.7,
                "market_id": "mkt-1",
            }
        )
        + "\n"
    )
    # Market resolved YES -> the tape's BUY_YES wins.
    monkeypatch.setattr(settler, "fetch_resolution", lambda mid, cache: "YES")
    monkeypatch.setattr(
        "sys.argv",
        ["settle_neutral_sitouts.py", "--input", str(shadow), "--output", str(settled)],
    )
    rc = settler.main()
    assert rc == 0
    rows = [json.loads(l) for l in settled.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["win"] is True
    assert rows[0]["outcome"] == "YES"
    assert rows[0]["realized_pct"] == 1.0  # (1-0.5)/0.5
