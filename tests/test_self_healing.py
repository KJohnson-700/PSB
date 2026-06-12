"""Unit tests for the Loop-3 self-healing supervisor (src/analysis/self_healing.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.analysis import self_healing as sh


NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _trade(lane_id, opened_at, win=True):
    return {"lane_id": lane_id, "side": "BUY_YES", "opened_at": opened_at,
            "closed_at": opened_at, "win": win}


# ── detectors ────────────────────────────────────────────────────────────────

def test_cold_lane_keys_off_lane_id_not_side_field(tmp_path):
    """side field is BUY_YES/BUY_NO; must key off lane_id part[2] (up/down)."""
    trades = tmp_path / "trades.jsonl"
    old = (NOW - timedelta(hours=40)).isoformat()
    _write_jsonl(trades, [_trade("bitcoin|1h|up|bullish|drift", old)])
    cfg = {"self_healing": {"cold_lane": {"enabled": True, "cold_hours": 24},
                            "auto_apply": {"enabled": False}}}
    cold = sh.detect_cold_lanes(config=cfg, now=NOW, trades_path=trades,
                                decision_log=tmp_path / "nope.jsonl")
    assert len(cold) == 1
    assert cold[0]["side"] == "up"            # derived from lane_id, not "buy_yes"
    assert cold[0]["cause"] == "structural"   # no AI veto, no overtight row
    assert cold[0]["auto_fixable"] is False


def test_cold_lane_recent_entry_not_flagged(tmp_path):
    trades = tmp_path / "trades.jsonl"
    recent = (NOW - timedelta(hours=2)).isoformat()
    _write_jsonl(trades, [_trade("eth_macro|15m|down|bear|drift", recent)])
    cfg = {"self_healing": {"cold_lane": {"enabled": True, "cold_hours": 24},
                            "auto_apply": {"enabled": False}}}
    cold = sh.detect_cold_lanes(config=cfg, now=NOW, trades_path=trades,
                                decision_log=tmp_path / "nope.jsonl")
    assert cold == []


def test_cold_lane_respects_strategy_scope(tmp_path):
    trades = tmp_path / "trades.jsonl"
    old = (NOW - timedelta(hours=40)).isoformat()
    _write_jsonl(
        trades,
        [
            _trade("doge_macro|15m|up|bullish|drift", old),
            _trade("sol_macro|15m|up|bullish|drift", old),
        ],
    )
    cfg = {
        "self_healing": {
            "scope": {"strategies": ["sol_macro"], "windows": ["15m"]},
            "cold_lane": {"enabled": True, "cold_hours": 24},
            "auto_apply": {"enabled": False},
        }
    }
    cold = sh.detect_cold_lanes(
        config=cfg,
        now=NOW,
        trades_path=trades,
        decision_log=tmp_path / "nope.jsonl",
    )
    assert [row["strategy"] for row in cold] == ["sol_macro"]


def test_cold_lane_respects_side_scope(tmp_path):
    trades = tmp_path / "trades.jsonl"
    old = (NOW - timedelta(hours=40)).isoformat()
    _write_jsonl(
        trades,
        [
            _trade("sol_macro|15m|up|bullish|drift", old),
            _trade("sol_macro|15m|down|bearish|drift", old),
        ],
    )
    cfg = {
        "self_healing": {
            "scope": {"strategies": ["sol_macro"], "windows": ["15m"], "sides": ["down"]},
            "cold_lane": {"enabled": True, "cold_hours": 24},
            "auto_apply": {"enabled": False},
        }
    }
    cold = sh.detect_cold_lanes(
        config=cfg,
        now=NOW,
        trades_path=trades,
        decision_log=tmp_path / "nope.jsonl",
    )
    assert [(row["strategy"], row["side"]) for row in cold] == [("sol_macro", "down")]


def test_wr_collapse_respects_trigger_scope(tmp_path):
    trades = tmp_path / "trades.jsonl"
    rows = []
    base = NOW - timedelta(days=10)
    for i in range(60):
        rows.append(_trade("hype_macro|15m|up|bull|drift", (base + timedelta(minutes=i)).isoformat(), win=True))
    for i in range(30):
        rows.append(_trade("hype_macro|15m|up|bull|drift", (base + timedelta(hours=2, minutes=i)).isoformat(), win=False))
    _write_jsonl(trades, rows)
    cfg = {
        "self_healing": {
            "scope": {"triggers": ["cold_lane"]},
            "wr_collapse": {"enabled": True, "window_n": 30, "min_sample": 25, "collapse_delta": 0.15},
        }
    }
    assert sh.detect_wr_collapse(config=cfg, trades_path=trades) == []


def test_cold_lane_ai_veto_attribution(tmp_path):
    trades = tmp_path / "trades.jsonl"
    old = (NOW - timedelta(hours=40)).isoformat()
    _write_jsonl(trades, [_trade("sol_macro|1h|up|bullish|drift", old)])
    decision = tmp_path / "decision_layer.jsonl"
    _write_jsonl(decision, [
        {"market_id": "m1", "ts_utc": (NOW - timedelta(hours=1)).isoformat(),
         "strategy": "sol_macro", "window": "1h", "quant_action": "BUY_YES",
         "approved": False, "fail_open": False, "reason": "low_confidence"},
    ])
    cfg = {"self_healing": {"cold_lane": {"enabled": True, "cold_hours": 24},
                            "auto_apply": {"enabled": False}}}
    cold = sh.detect_cold_lanes(config=cfg, now=NOW, trades_path=trades, decision_log=decision)
    assert cold[0]["cause"] == "ai_veto"
    assert cold[0]["auto_fixable"] is False


def test_cold_lane_ai_unavailable_attribution(tmp_path):
    trades = tmp_path / "trades.jsonl"
    old = (NOW - timedelta(hours=40)).isoformat()
    _write_jsonl(trades, [_trade("sol_macro|1h|up|bullish|drift", old)])
    decision = tmp_path / "decision_layer.jsonl"
    _write_jsonl(decision, [
        {"market_id": "m1", "ts_utc": (NOW - timedelta(hours=1)).isoformat(),
         "strategy": "sol_macro", "window": "1h", "quant_action": "BUY_YES",
         "approved": False, "fail_open": False, "reason": "provider unavailable timeout"},
    ])
    cfg = {"self_healing": {"cold_lane": {"enabled": True, "cold_hours": 24},
                            "auto_apply": {"enabled": False}}}
    cold = sh.detect_cold_lanes(config=cfg, now=NOW, trades_path=trades, decision_log=decision)
    assert cold[0]["cause"] == "ai_unavailable"


def test_wr_collapse_vs_chronic(tmp_path):
    trades = tmp_path / "trades.jsonl"
    rows = []
    base = NOW - timedelta(days=10)
    # 60 baseline wins (high WR), then 30 recent losses (collapse).
    for i in range(60):
        rows.append(_trade("hype_macro|15m|up|bull|drift",
                            (base + timedelta(minutes=i)).isoformat(), win=True))
    for i in range(30):
        rows.append(_trade("hype_macro|15m|up|bull|drift",
                            (base + timedelta(hours=2, minutes=i)).isoformat(), win=False))
    _write_jsonl(trades, rows)
    cfg = {"self_healing": {"wr_collapse": {"enabled": True, "window_n": 30,
                                            "min_sample": 25, "collapse_delta": 0.15}}}
    out = sh.detect_wr_collapse(config=cfg, trades_path=trades)
    assert len(out) == 1
    assert out[0]["recent_wr"] < out[0]["baseline_wr"]
    assert out[0]["drop"] >= 0.15


# ── auto-apply: loosen-only + TTL ────────────────────────────────────────────

def _cold_with_overtight(mult):
    return {
        "strategy": "hype_macro", "window": "15m", "side": "up",
        "age_hours": 30.0, "cause": "quant_gate", "auto_fixable": True,
        "overtight": [{
            "lane_id": "hype_macro|15m|up|bullish",
            "recommended_min_edge_mult": mult, "ghost_n": 40, "ghost_win_rate": 0.80,
            "admitted_n": 30, "admitted_win_rate": 0.78, "recommended_relax_delta": 0.02,
            "verdict": "OVERTIGHT",
        }],
    }


def test_auto_apply_loosen_injects_runtime_feedback(tmp_path):
    cfg = {"self_healing": {"auto_apply": {"enabled": True, "ttl_hours": 12,
                                           "min_edge_mult_floor": 0.70}}}
    applied = sh.apply_auto_heal(cfg, [_cold_with_overtight(0.715)], now=NOW,
                                 actions_log=tmp_path / "actions.jsonl")
    assert len(applied) == 1
    row = cfg["_runtime_feedback"]["by_lane"]["hype_macro|15m|up|bullish"]
    assert row["source"] == "self_healing"
    assert row["min_edge_mult"] == pytest.approx(0.715)
    assert applied[0]["action_class"] == "auto_loosen"
    assert applied[0]["editor_action"] == "monitor_runtime_override"


def test_auto_apply_never_tightens(tmp_path):
    """Loosen-only invariant: a mult > 1.0 must be rejected."""
    cfg = {"self_healing": {"auto_apply": {"enabled": True, "ttl_hours": 12,
                                           "min_edge_mult_floor": 0.70}}}
    applied = sh.apply_auto_heal(cfg, [_cold_with_overtight(1.30)], now=NOW,
                                 actions_log=tmp_path / "actions.jsonl")
    assert applied == []
    assert not cfg.get("_runtime_feedback", {}).get("by_lane")


def test_auto_apply_respects_floor(tmp_path):
    cfg = {"self_healing": {"auto_apply": {"enabled": True, "ttl_hours": 12,
                                           "min_edge_mult_floor": 0.70}}}
    sh.apply_auto_heal(cfg, [_cold_with_overtight(0.50)], now=NOW,
                       actions_log=tmp_path / "actions.jsonl")
    assert cfg["_runtime_feedback"]["by_lane"]["hype_macro|15m|up|bullish"]["min_edge_mult"] == 0.70


def test_reapply_after_clobber_and_ttl_prune(tmp_path):
    cfg = {"self_healing": {"auto_apply": {"enabled": True, "ttl_hours": 12,
                                           "min_edge_mult_floor": 0.70}}}
    sh.apply_auto_heal(cfg, [_cold_with_overtight(0.715)], now=NOW,
                       actions_log=tmp_path / "actions.jsonl")
    # perf-feedback clobbers _runtime_feedback every refresh.
    cfg["_runtime_feedback"] = {"enabled": True, "by_lane": {}, "by_strategy": {}}
    assert sh.reapply_active_overrides(cfg, NOW) == 1
    assert "hype_macro|15m|up|bullish" in cfg["_runtime_feedback"]["by_lane"]
    # past TTL -> pruned.
    assert sh.reapply_active_overrides(cfg, NOW + timedelta(hours=13)) == 0
    assert not cfg[sh._OVERRIDES_KEY]


# ── escalation: dedupe ───────────────────────────────────────────────────────

def test_escalation_packet_has_action_taxonomy():
    packet = sh.build_escalation_packet(
        trigger="cold_lane:ai_unavailable",
        lane={"strategy": "sol_macro", "window": "1h", "side": "up"},
        now=NOW,
        ghost_validatable=False,
        diagnosis="provider timeout",
    )
    assert packet["action_class"] == "ops_alert"
    assert packet["severity"] == "high"
    assert packet["editor_action"] == "check_ai_provider_health"
    assert packet["auto_apply_allowed"] is False
    assert "next_steps" in packet
    assert "Auto-apply is limited" in packet["recommendation_contract"]


def test_escalation_dedupe_same_day(tmp_path):
    trades = tmp_path / "trades.jsonl"
    old = (NOW - timedelta(hours=40)).isoformat()
    _write_jsonl(trades, [_trade("bitcoin|1h|up|bullish|drift", old)])
    qdir = tmp_path / "esc"
    cfg = {
        "performance_feedback": {"enabled": False},
        "self_healing": {
            "enabled": True,
            "cold_lane": {"enabled": True, "cold_hours": 24},
            "wr_collapse": {"enabled": False},
            "auto_apply": {"enabled": False},
            "escalation": {"queue_dir": str(qdir), "notify": False, "codex_dispatch": False},
        },
    }
    # Patch detector inputs by monkeypatching module defaults is overkill; call run twice.
    sup = sh.SelfHealingSupervisor(cfg)
    # Point the detector at our fixture trades via module default override.
    orig = sh.DEFAULT_TRADES_LOG
    sh.DEFAULT_TRADES_LOG = trades
    try:
        r1 = sup.run(now=NOW)
        r2 = sup.run(now=NOW)
    finally:
        sh.DEFAULT_TRADES_LOG = orig
    assert len(r1.escalations) >= 1
    assert len(r2.escalations) == 0  # deduped within the same day
