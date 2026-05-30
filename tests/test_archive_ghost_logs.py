"""Loop-safety tests for the ghost-log archival script (spec step 3c).

The one invariant that must never break: an UNSETTLED ghost is never archived,
and after archival the settle loop still settles exactly the rows it would have,
with no duplicates.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scripts.archive_ghost_logs as arch
import src.analysis.ghost_calibration as gc


def _row(tag, days_old, **extra):
    """A rejected-candidate row. NOTE: no literal ghost_id — production rejected
    rows don't carry one; identity is the canonical sha1(ts|market_id|reason)."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    r = {"ts": ts, "market_id": tag, "reason": "r", "action": "BUY_YES"}
    r.update(extra)
    return r


def _settled_of(rej_row, **extra):
    """Build the settled counterpart of a rejected row, exactly as the settle
    loop would: literal ghost_id == canonical id of the rejected row."""
    s = dict(rej_row)
    s["ghost_id"] = gc.ghost_id(rej_row)
    s.setdefault("win", True)
    s.update(extra)
    return s


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_unsettled_old_row_never_archived(tmp_path):
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    # old_settled is old AND settled; old_unsettled is old but NOT settled;
    # recent is within cutoff.
    old_settled = _row("old_settled", 10)
    old_unsettled = _row("old_unsettled", 10)
    recent = _row("recent", 0)
    _write(rej, [old_settled, old_unsettled, recent])
    _write(settled, [_settled_of(old_settled)])

    s = arch.archive_ghost_logs(
        rejected_path=rej, settled_path=settled, index_path=idx,
        cutoff_days=3, execute=True,
    )
    kept_mids = {json.loads(l)["market_id"] for l in rej.read_text().splitlines() if l.strip()}
    # The unsettled old row and the recent row stay; only old+settled is archived.
    assert "old_unsettled" in kept_mids
    assert "recent" in kept_mids
    assert "old_settled" not in kept_mids
    assert s["rejected_archived"] == 1
    # Nothing lost: kept + archived == original total.
    assert s["rejected_kept"] + s["rejected_archived"] == 3


def test_archive_roundtrip_and_no_loss(tmp_path):
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    rows = [_row(f"g{i}", 10) for i in range(6)]
    _write(rej, rows)
    _write(settled, [_settled_of(r, win=(i % 2 == 0)) for i, r in enumerate(rows)])

    s = arch.archive_ghost_logs(
        rejected_path=rej, settled_path=settled, index_path=idx,
        cutoff_days=3, execute=True,
    )
    # All old + settled → all archived; live files now empty.
    assert s["settled_archived"] == 6
    arch_files = list((tmp_path / "archive").glob("*.jsonl.gz"))
    total = 0
    for a in arch_files:
        with gzip.open(a, "rt") as gz:
            total += sum(1 for line in gz if line.strip())
    assert total == 12  # 6 rejected + 6 settled
    # .bak preserved
    assert (rej.with_name(rej.name + ".pre-archive.bak")).exists()
    assert (settled.with_name(settled.name + ".pre-archive.bak")).exists()


def test_loop_stays_closed_after_archive(tmp_path, monkeypatch):
    """The closed-loop guarantee across archival: once a ghost is settled and its
    rows are archived out of the live files, the loop must NEVER re-settle or
    re-fetch it — even if the same rejected row reappears in the live log. The
    settled_index (which retains every ghost_id) is what protects it."""
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    monkeypatch.setattr(gc, "load_regime_snapshots", lambda path=None: [])
    monkeypatch.setattr(gc, "fetch_resolution", lambda mid, cache: "YES")

    # Settle some rows first (creates settled file + index).
    end = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    rows = [_row(str(i), 10, market_end_ts=end) for i in range(4)]
    _write(rej, rows)
    gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert sum(1 for _ in gc._iter_jsonl(settled)) == 4

    # Archive old+settled rows out of BOTH live files.
    s_arch = arch.archive_ghost_logs(
        rejected_path=rej, settled_path=settled, index_path=idx,
        cutoff_days=3, execute=True,
    )
    assert s_arch["rejected_archived"] == 4
    assert s_arch["settled_archived"] == 4
    # Index still retains all 4 ghost_ids despite the live files being emptied.
    assert len(gc._load_settled_ids_indexed(settled, idx)) == 4

    # Simulate a duplicate/replayed rejected row landing back in the live log
    # (e.g. a backfill re-append). The loop must recognize it via the index and
    # NOT re-fetch — fetch stub raises if called.
    _write(rej, [rows[0]])
    def _boom(mid, cache):
        raise AssertionError("re-fetched an archived market — loop reopened!")
    monkeypatch.setattr(gc, "fetch_resolution", _boom)
    s = gc.settle_rejected_candidates(input_path=rej, output_path=settled, index_path=idx)
    assert s["newly_settled"] == 0
    assert s["already_settled"] == 1


def test_dry_run_changes_nothing(tmp_path):
    rej = tmp_path / "rej.jsonl"
    settled = tmp_path / "set.jsonl"
    idx = tmp_path / "idx.txt"
    rows = [_row(f"g{i}", 10, win=True) for i in range(3)]
    _write(rej, rows)
    _write(settled, rows)
    before_rej = rej.read_text()
    before_set = settled.read_text()

    s = arch.archive_ghost_logs(
        rejected_path=rej, settled_path=settled, index_path=idx,
        cutoff_days=3, execute=False,
    )
    assert s["execute"] is False
    assert rej.read_text() == before_rej  # untouched
    assert settled.read_text() == before_set
    assert not (tmp_path / "archive").exists()
