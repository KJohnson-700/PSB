"""Regression tests for the 2026-08-15 CENSORED-SAMPLE P0 (Codex P1-D).

THE BUG: adaptive_lane_sizer read trades_settled.jsonl, which holds exit_reason "updown_expired"
ONLY. Stopped trades never entered it, so the sizer trained on a winner-biased sample and
over-rated any lane in proportion to how often it stopped out — four lanes read 100% WR
(sol|1h|YES n=11 wr=100.0 mult 1.4796 while its truth was 29 trades / 51.7%).

These tests lock in the three properties that make that impossible to reintroduce:
  1. STOPPED trades reach lane stats (the fix itself).
  2. The loader NEVER silently falls back to the censored file (Codex P1-C).
  3. Ineligible rows (open / shadow / foreign-mode / duplicate) never train the sizer (P1-A).

Run: .venv/bin/python -m pytest tests/test_adaptive_sizer_censored_source.py -q
"""
import json

import src.analysis.adaptive_lane_sizer as S


def _row(tid, action="BUY_NO", pnl=-5.0, **kw):
    r = {
        "trade_id": tid, "session_id": "sess_a", "ts": "2026-08-15T00:00:00+00:00",
        "closed_at": "2026-08-15T00:05:00+00:00", "strategy": "xrp_macro", "window": "5m",
        "action": action, "pnl": pnl, "notional": 12.0, "shadow_mode": False,
        "exit_reason": "hold_catastrophic_stop",
    }
    r.update(kw)
    return r


def _write(p, rows):
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# ── 1. THE FIX: stopped trades must reach lane stats ─────────────────────────
def test_stopped_trades_are_included(tmp_path, monkeypatch):
    full = tmp_path / "trades.jsonl"
    _write(full, [
        _row("t1", pnl=+10.0, exit_reason="updown_expired"),   # the only kind the OLD source had
        _row("t2", pnl=-9.0, exit_reason="hold_catastrophic_stop"),
        _row("t3", pnl=-8.0, exit_reason="updown_stop_loss"),
        _row("t4", pnl=-7.0, exit_reason="never_green_cut"),
    ])
    monkeypatch.setattr(S, "FULL_JOURNAL_PATH", full)
    rows = S._load_rows(full)
    assert len(rows) == 4, "stopped trades must not be dropped"
    reasons = {r["exit_reason"] for r in rows}
    assert "hold_catastrophic_stop" in reasons and "updown_stop_loss" in reasons
    # and the censoring signature is gone: WR is NOT 100%
    wins = sum(1 for r in rows if r["actual_pnl"] > 0)
    assert wins / len(rows) == 0.25, "a 1W/3L lane must read 25%, not 100%"


def test_normalization_maps_pnl_and_notional(tmp_path):
    full = tmp_path / "trades.jsonl"
    _write(full, [_row("t1", pnl=-3.5)])
    r = S._load_rows(full)[0]
    assert r["actual_pnl"] == -3.5, "pnl must map onto actual_pnl"
    assert r["cost_basis"] == 12.0, "notional must map onto cost_basis"


def test_settled_rows_still_work_unchanged(tmp_path):
    """The legacy schema must keep working — its own field names win."""
    settled = tmp_path / "trades_settled.jsonl"
    _write(settled, [{
        "trade_id": "s1", "session_id": "sess_a", "ts": "2026-08-15T00:00:00+00:00",
        "closed_at": "2026-08-15T00:05:00+00:00", "strategy": "xrp_macro", "window": "5m",
        "action": "BUY_NO", "actual_pnl": -2.0, "cost_basis": 11.0,
    }])
    r = S._load_rows(settled)[0]
    assert r["actual_pnl"] == -2.0 and r["cost_basis"] == 11.0


# ── 2. Codex P1-C: NEVER fail open into the censored file ────────────────────
def test_missing_full_source_returns_empty_not_censored(tmp_path, monkeypatch):
    """If trades.jsonl is missing we must return [] — NOT silently read the censored file,
    which would restore the exact over-rating defect with no visible failure."""
    missing_full = tmp_path / "trades.jsonl"          # deliberately absent
    settled = tmp_path / "trades_settled.jsonl"
    _write(settled, [_row("s1", pnl=+9.0, exit_reason="updown_expired")])
    monkeypatch.setattr(S, "FULL_JOURNAL_PATH", missing_full)
    monkeypatch.setattr(S, "SETTLED_PATH", settled)
    assert S._load_rows(missing_full) == [], "must NOT fall back to the censored source"


def test_missing_settled_source_falls_back_to_full(tmp_path, monkeypatch):
    """The other direction IS safe: settled -> full is a fallback to the uncensored superset."""
    full = tmp_path / "trades.jsonl"
    missing_settled = tmp_path / "trades_settled.jsonl"   # absent
    _write(full, [_row("t1"), _row("t2")])
    monkeypatch.setattr(S, "FULL_JOURNAL_PATH", full)
    monkeypatch.setattr(S, "SETTLED_PATH", missing_settled)
    assert len(S._load_rows(missing_settled)) == 2


# ── 3. Codex P1-A: ineligible rows must never train the sizer ────────────────
def test_ineligible_rows_are_excluded(tmp_path):
    full = tmp_path / "trades.jsonl"
    _write(full, [
        _row("keep1"),
        _row("open1", pnl=None),                       # OPEN — no realized pnl
        _row("shadow1", shadow_mode=True),             # shadow must never train live sizing
        _row("mode1", mode="backtest"),                # explicit foreign mode
        _row("keep2", mode="paper"),                   # explicit good mode
        _row("keep3", mode=None),                      # legacy row: mode unset -> KEEP
        _row("keep1"),                                 # duplicate trade_id -> counted once
    ])
    rows = S._load_rows(full)
    ids = [r["trade_id"] for r in rows]
    assert ids == ["keep1", "keep2", "keep3"], ids


# ── 4. the config knob resolves, and the revert path works ───────────────────
def test_source_knob_and_revert():
    assert S._source_path({"source": "full"}) == S.FULL_JOURNAL_PATH
    assert S._source_path({"source": "settled"}) == S.SETTLED_PATH
    assert S._source_path({}) == S.FULL_JOURNAL_PATH, "default must be the UNCENSORED source"
    assert S._cfg({"trading": {"adaptive_sizer": {"source": "settled"}}})["source"] == "settled", \
        "source must survive the DEFAULTS filter in _cfg (else it is a silent no-op)"
