"""Guards the 2026-06-18 OOM fix: settled-id loading must NOT full-parse rows.

The settled-index rebuild over the multi-hundred-MB settled jsonl (+ archive
shards) was full-parsing every fat nested row, ballooning pymalloc arenas to
~4-5 GB (Jetsam kill). _load_settled_ids now lifts the top-level ghost_id from
the raw line via regex, falling back to a full parse only when the regex misses.
"""
import json
from src.analysis.ghost_calibration import _load_settled_ids, _QGID_SETTLED


def test_quick_extract_matches_ghost_id():
    line = json.dumps({"ghost_id": "abc123def456", "reason": "x", "win": True, "nested": {"a": [1, 2]}})
    m = _QGID_SETTLED.search(line)
    assert m and m.group(1) == "abc123def456"


def test_load_settled_ids_covers_regex_and_fallback(tmp_path):
    p = tmp_path / "settled.jsonl"
    rows = [
        {"ghost_id": "id_regex_1", "reason": "lane_min_edge", "win": True},
        {"ghost_id": "id_regex_2", "reason": "neutral_bias", "win": False},
        # ghost_id present but not first/simple — regex still finds it
        {"reason": "x", "nested": {"k": "v"}, "ghost_id": "id_regex_3"},
        # row with no ghost_id — must be skipped, not crash
        {"reason": "no_id_here", "win": True},
    ]
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        # malformed line — must be skipped
        f.write("{not json\n")
    ids = _load_settled_ids(p)
    assert ids == {"id_regex_1", "id_regex_2", "id_regex_3"}


def test_load_settled_ids_missing_file(tmp_path):
    assert _load_settled_ids(tmp_path / "nope.jsonl") == set()
