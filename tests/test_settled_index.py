"""Closed-loop equivalence + robustness tests for the settled-index sidecar.

Step 3a of docs/GHOST_LOG_CHECKPOINT_SPEC.md. The non-negotiable invariant is
that introducing the index changes performance only — never *what* gets settled.
These tests prove the indexed settle path produces an identical settled set and
β feed to the original full-scan path, and that the index self-heals.
"""
from __future__ import annotations

import json
from pathlib import Path

import src.analysis.ghost_calibration as gc


def _market(mid: str, action: str = "BUY_YES", reason: str = "r", ts: str = "2026-05-20T10:00:00+00:00"):
    return {
        "ts": ts,
        "market_id": mid,
        "reason": reason,
        "action": action,
        "yes_price": 0.40,
        "no_price": 0.60,
        "lane_id": f"bitcoin|5m|up|bullish|rejected",
        "strategy": "bitcoin",
        "window": "5m",
        "side": "LONG",
        "market_end_ts": "2026-05-20T09:00:00+00:00",  # already past → not too_recent
    }


def _write_jsonl(path: Path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class _RecordingCalibrator:
    def __init__(self):
        self.calls = []

    def record_ghost(self, lane_id, win, weight=0.5):
        self.calls.append((lane_id, bool(win), weight))


def _resolutions(mapping):
    """Return a fetch_resolution stub backed by a dict, ignoring the cache arg."""
    def _stub(market_id, cache):
        return mapping.get(str(market_id))
    return _stub


def test_indexed_matches_fullscan(tmp_path, monkeypatch):
    """Indexed settle must produce the same settled rows + β feed as full-scan."""
    rows = [_market(str(i), action=("BUY_YES" if i % 2 else "BUY_NO")) for i in range(10)]
    res = {str(i): ("YES" if i % 3 else "NO") for i in range(10)}
    monkeypatch.setattr(gc, "fetch_resolution", _resolutions(res))
    monkeypatch.setattr(gc, "load_regime_snapshots", lambda path=None: [])

    # --- full-scan path (use_index=False) ---
    rej_a = tmp_path / "rej_a.jsonl"
    set_a = tmp_path / "set_a.jsonl"
    _write_jsonl(rej_a, rows)
    cal_a = _RecordingCalibrator()
    s_a = gc.settle_rejected_candidates(
        input_path=rej_a, output_path=set_a, use_index=False,
        calibrator=cal_a,
    )

    # --- indexed path (use_index=True) ---
    rej_b = tmp_path / "rej_b.jsonl"
    set_b = tmp_path / "set_b.jsonl"
    idx_b = tmp_path / "idx_b.txt"
    _write_jsonl(rej_b, rows)
    cal_b = _RecordingCalibrator()
    s_b = gc.settle_rejected_candidates(
        input_path=rej_b, output_path=set_b, index_path=idx_b, use_index=True,
        calibrator=cal_b,
    )

    assert s_a["newly_settled"] == s_b["newly_settled"] == 10
    # Settled ghost_ids identical
    ids_a = {r["ghost_id"] for r in gc._iter_jsonl(set_a)}
    ids_b = {r["ghost_id"] for r in gc._iter_jsonl(set_b)}
    assert ids_a == ids_b
    # Outcomes identical per ghost_id
    out_a = {r["ghost_id"]: (r["outcome"], r.get("win")) for r in gc._iter_jsonl(set_a)}
    out_b = {r["ghost_id"]: (r["outcome"], r.get("win")) for r in gc._iter_jsonl(set_b)}
    assert out_a == out_b
    # β feed identical (order-independent)
    assert sorted(cal_a.calls) == sorted(cal_b.calls)
    # Index written and consistent with settled file
    assert idx_b.exists()
    assert {l.strip() for l in idx_b.read_text().splitlines() if l.strip()} == ids_b


def test_idempotent_second_pass(tmp_path, monkeypatch):
    """Running twice settles nothing new and appends nothing (loop stays closed)."""
    rows = [_market(str(i)) for i in range(5)]
    monkeypatch.setattr(gc, "fetch_resolution", _resolutions({str(i): "YES" for i in range(5)}))
    monkeypatch.setattr(gc, "load_regime_snapshots", lambda path=None: [])
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    _write_jsonl(rej, rows)

    s1 = gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    s2 = gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert s1["newly_settled"] == 5
    assert s2["newly_settled"] == 0
    assert s2["already_settled"] == 5
    # settled file has exactly 5 rows (no duplicates)
    assert sum(1 for _ in gc._iter_jsonl(settled)) == 5


def test_pending_then_resolves(tmp_path, monkeypatch):
    """A row unresolved on pass 1 must still settle on pass 2 — nothing lost."""
    rows = [_market("100")]
    _write_jsonl(tmp_path / "rej.jsonl", rows)
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    monkeypatch.setattr(gc, "load_regime_snapshots", lambda path=None: [])

    monkeypatch.setattr(gc, "fetch_resolution", _resolutions({}))  # unresolved
    s1 = gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert s1["newly_settled"] == 0
    assert s1["unresolved_or_api"] == 1

    monkeypatch.setattr(gc, "fetch_resolution", _resolutions({"100": "YES"}))  # now resolves
    s2 = gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert s2["newly_settled"] == 1


def test_index_rebuilds_when_missing(tmp_path, monkeypatch):
    """If the index is deleted but the settled file has rows, the loop rebuilds it
    and does NOT re-settle (no Gamma re-fetch, no duplicate rows)."""
    rows = [_market(str(i)) for i in range(4)]
    _write_jsonl(tmp_path / "rej.jsonl", rows)
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    monkeypatch.setattr(gc, "load_regime_snapshots", lambda path=None: [])
    monkeypatch.setattr(gc, "fetch_resolution", _resolutions({str(i): "YES" for i in range(4)}))

    gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert idx.exists()

    # Nuke the index + meta — simulate corruption / loss.
    idx.unlink()
    meta = gc._settled_index_meta_path(idx)
    if meta.exists():
        meta.unlink()

    # A fetch stub that would RAISE if called — proves we did NOT re-fetch.
    def _boom(market_id, cache):
        raise AssertionError("re-fetched an already-settled market — loop reopened!")
    monkeypatch.setattr(gc, "fetch_resolution", _boom)

    s = gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert s["newly_settled"] == 0
    assert s["already_settled"] == 4
    assert idx.exists()  # rebuilt
    assert sum(1 for _ in gc._iter_jsonl(settled)) == 4  # no duplicates


def test_rotation_truncation_self_heals(tmp_path, monkeypatch):
    """If the settled file is replaced (rotation/truncation), the stale index is
    discarded and rebuilt from the current file — idempotency preserved."""
    rows = [_market(str(i)) for i in range(3)]
    _write_jsonl(tmp_path / "rej.jsonl", rows)
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    monkeypatch.setattr(gc, "load_regime_snapshots", lambda path=None: [])
    monkeypatch.setattr(gc, "fetch_resolution", _resolutions({str(i): "YES" for i in range(3)}))
    gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)

    # Replace the settled file with a smaller one (truncation): index now stale.
    first = next(gc._iter_jsonl(settled))
    _write_jsonl(settled, [first])

    ids = gc._load_settled_ids_indexed(settled, idx)
    # Must reflect the CURRENT settled file (1 row), not the stale cached 3.
    assert ids == {first["ghost_id"]}
