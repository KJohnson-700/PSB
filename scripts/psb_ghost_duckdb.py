#!/usr/bin/env python3
"""Ghost calibration -> DuckDB (Option C: durable, compact, indexed store).

The ghost logs (rejected_candidates_settled.jsonl + archives) are ~0.8GB+ of
newline-delimited JSON the analyses scan end-to-end every time. This builds a
columnar DuckDB so:
  * disk shrinks ~10-20x (columnar + compression),
  * lane EV / WR cuts are an indexed SQL query instead of an 0.8GB file scan,
  * history is retained indefinitely at trivial cost.

ADDITIVE / SAFE: this never modifies the JSONL. The bot keeps appending JSONL as
the source of truth; this job ingests new rows incrementally (anti-join on
ghost_id) into data/calibration/ghost.duckdb. Reads .jsonl AND .jsonl.gz so the
cold archive (from psb_data_lifecycle) stays queryable.

Usage:
    python scripts/psb_ghost_duckdb.py --ingest          # incremental load
    python scripts/psb_ghost_duckdb.py --stats           # row counts + db size
    python scripts/psb_ghost_duckdb.py --query "SELECT strategy, count(*) ..."
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CAL = REPO / "data" / "calibration"
DB_PATH = CAL / "ghost.duckdb"

# Curated top-level columns the lane analyses actually use. The full row is kept
# as raw JSON so nothing is lost and ad-hoc fields stay queryable via json_extract.
COLUMNS = {
    "ghost_id": "VARCHAR",
    "ts": "VARCHAR",
    "settled_at": "VARCHAR",
    "strategy": "VARCHAR",
    "window": "VARCHAR",
    "side": "VARCHAR",
    "action": "VARCHAR",
    "outcome": "VARCHAR",
    "win": "BOOLEAN",
    "realized_pct": "DOUBLE",
    "yes_price": "DOUBLE",
    "est_prob_up": "DOUBLE",
    "htf_bias": "VARCHAR",
    "btc_1h_regime": "VARCHAR",
    "convergence_score": "DOUBLE",
    "reason": "VARCHAR",
    "market_id": "VARCHAR",
}


def _sources(live_only: bool = False) -> list[str]:
    """Settled JSONL sources. live_only=True returns ONLY the live file (the cron
    path): new rows append there, archives are static and already ingested, so
    re-reading the gz archives every 5 min was the slow ingest (lock hog). The full
    rebuild path (live_only=False) reads archives too."""
    pats = [str(CAL / "rejected_candidates_settled.jsonl")]
    if not live_only:
        pats += [
            str(CAL / "archive" / "rejected_candidates_settled*.jsonl"),
            str(CAL / "archive" / "rejected_candidates_settled*.jsonl.gz"),
        ]
    return [p for p in pats if list(Path(p).parent.glob(Path(p).name))]


def ingest(con, live_only: bool = False) -> dict:
    cols_sql = ", ".join(f'"{k}" {v}' for k, v in COLUMNS.items())
    # Curated scalar columns only — no raw JSON copy (that would defeat the size
    # win). The JSONL stays the source of truth for rare nested fields.
    con.execute(
        f"CREATE TABLE IF NOT EXISTS ghost_settled ({cols_sql});"
    )
    # Self-healing schema: ALTER only columns ACTUALLY missing. Running ADD COLUMN
    # for all 16 cols every ingest forced a checkpoint/rewrite (the slow lock hog).
    existing = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='ghost_settled'"
        ).fetchall()
    }
    for _col, _typ in COLUMNS.items():
        if _col not in existing:
            con.execute(f'ALTER TABLE ghost_settled ADD COLUMN "{_col}" {_typ};')
    con.execute(
        'CREATE INDEX IF NOT EXISTS idx_lane ON ghost_settled("strategy", "window", "side");'
    )
    before = con.execute("SELECT count(*) FROM ghost_settled").fetchone()[0]

    select_cols = ", ".join(
        f"TRY_CAST(json_extract_string(j, '$.{k}') AS {v}) AS \"{k}\""
        for k, v in COLUMNS.items()
    )
    # 2026-08-14 CORRUPTION FIX — the INSERT below MUST name its target columns.
    # It used to be a bare `INSERT INTO ghost_settled SELECT ...`, which assigns
    # POSITIONALLY against the table's physical column order. The table on disk was
    # created 2026-06-17 ending (..., reason, market_id, convergence_score) while
    # COLUMNS in this file ends (..., convergence_score, reason, market_id). Values
    # were therefore rotated by one for the last three columns on every insert:
    # reason -> market_id, market_id -> convergence_score. A catch-up ingest wrote
    # 812,267 rows where market_id held strings like
    # 'buy_no_5m_oversold_hard_floor(rsi=36<40)'. count(distinct market_id) reported
    # 296 markets for August instead of ~28,400 — silent, and it would have poisoned
    # every dedupe-by-market analysis downstream.
    # Naming the columns makes the insert order-independent, so the table's physical
    # order and this dict can drift again without corrupting anything.
    insert_cols = ", ".join(f'"{k}"' for k in COLUMNS)
    for src in _sources(live_only=live_only):
        # read_json_objects returns one column ('json') holding each line's whole
        # JSON object; extract the curated scalar fields from it.
        con.execute(
            f"""
            INSERT INTO ghost_settled ({insert_cols})
            SELECT {select_cols}
            FROM (
                SELECT json AS j FROM read_json_objects('{src}',
                    format='newline_delimited',
                    ignore_errors=true,
                    maximum_object_size=20000000)
            ) AS s
            -- NULL-safe anti-join (NOT IN breaks if any existing ghost_id is NULL)
            WHERE NOT EXISTS (
                SELECT 1 FROM ghost_settled g
                WHERE g.ghost_id = json_extract_string(s.j, '$.ghost_id')
            );
            """
        )
    after = con.execute("SELECT count(*) FROM ghost_settled").fetchone()[0]
    return {"rows_before": before, "rows_after": after, "inserted": after - before}


def stats(con) -> None:
    n = con.execute("SELECT count(*) FROM ghost_settled").fetchone()[0]
    size_mb = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0
    print(f"ghost.duckdb: {n:,} rows, {size_mb:.1f} MB on disk")
    src_mb = sum(
        f.stat().st_size for f in CAL.glob("rejected_candidates_settled*.jsonl")
    ) / 1024 / 1024
    if src_mb:
        print(f"  vs live JSONL source: {src_mb:.0f} MB  ({src_mb / max(size_mb,0.1):.0f}x smaller)")
    print("  per-strategy:")
    for row in con.execute(
        "SELECT strategy, count(*) n, round(avg(realized_pct),4) avg_ret "
        "FROM ghost_settled GROUP BY strategy ORDER BY n DESC"
    ).fetchall():
        print(f"    {row[0] or '?':14} n={row[1]:>7,}  avg_realized={row[2]}")


def main() -> int:
    try:
        import duckdb
    except ImportError:
        print("duckdb not installed: .venv/bin/pip install duckdb", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description="Ghost calibration DuckDB store")
    ap.add_argument("--ingest", action="store_true", help="incremental load JSONL -> DuckDB")
    ap.add_argument("--live-only", action="store_true", help="ingest only the live JSONL (cron path; skip static archives)")
    ap.add_argument("--stats", action="store_true", help="row counts + size")
    ap.add_argument("--query", type=str, help="run a read-only SQL query against ghost_settled")
    args = ap.parse_args()

    CAL.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    try:
        if args.ingest:
            r = ingest(con, live_only=args.live_only)
            print(f"ingest: +{r['inserted']:,} rows (total {r['rows_after']:,})")
        if args.query:
            for row in con.execute(args.query).fetchall():
                print(row)
        if args.stats or not (args.ingest or args.query):
            stats(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
