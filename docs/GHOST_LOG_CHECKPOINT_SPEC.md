# Spec: Ghost-Log Checkpoint + Archival (reclaim 1.5 GB, keep the loop closed)

Status: **3a SHIPPED + 3c SHIPPED (script, dry-run default).** 3b deferred.
Date: 2026-05-29.

## Implementation status
- **3a — `settled_index.txt` sidecar: DONE.** `src/analysis/ghost_calibration.py`
  (`_load_settled_ids_indexed` + helpers); `settle_rejected_candidates(use_index=True)`
  default-on with self-healing full rebuild. Tests: `tests/test_settled_index.py` (5).
- **3c — archival script: DONE (not yet executed on real data).**
  `scripts/archive_ghost_logs.py`, **dry-run by default**, `--execute` to apply.
  Tests: `tests/test_archive_ghost_logs.py` (4). A live dry-run reports ~216K of 337K
  rows (rows older than 3 days) are archivable from EACH file.
- **3b — forward cursor on the rejected file: DEFERRED.** 3a removed the large per-cycle
  read (the settled scan); the rejected scan remains but is bounded and far cheaper.
  3b is a further optimization, not required for the closed loop or the size win.

### Closed-loop hardening discovered during implementation
Archival shrinks the live settled file, which is indistinguishable from
truncation/corruption by inode+size alone. Two safeguards make archival loop-safe:
1. Index rebuild scans **live settled jsonl + compressed archive shards** as the
   combined source of truth (`_iter_archived_settled_ids`), so archived ghost_ids are
   never forgotten.
2. `archive_ghost_logs` rewrites the index to the full settled set and re-points its
   meta after shrinking the live file, so the next load doesn't mistake the shrink for
   corruption. Proven by `test_loop_stays_closed_after_archive`.

## Goal

Reclaim the existing ~1.5 GB in `rejected_candidates.jsonl` (721 MB) +
`rejected_candidates_settled.jsonl` (757 MB), and make the settle loop O(new rows)
instead of O(whole file) — **without ever reopening the closed loop**. This is an
*optimization + robustness* change, not a data cut: the JSONL files remain the
source of truth; everything new is a derived, rebuildable cache.

## Non-negotiable invariant

> The ghost loop must stay closed. Every rejected candidate must still get settled
> exactly once against the real Polymarket outcome, and every settled outcome must
> still reach the calibrator/aggregators. The optimization may only change *how fast*
> and *how much we re-read*, never *what gets settled*.

**Safety mechanism that guarantees this:** every derived structure (index, cursor,
pending queue) is **reconstructable from the JSONL source of truth**. If any checkpoint
is missing, stale, or fails a consistency check, the loop **falls back to today's
full-scan behavior and rebuilds the checkpoint from scratch**. Worst case = as slow as
today. It can never get permanently stuck or skip a candidate.

## Current behavior (what we're optimizing)

`settle_rejected_candidates()` — `src/analysis/ghost_calibration.py:536`:

1. `settled_ids = _load_settled_ids(output_path)` (`:573`, impl `:111`) — reads **every
   ghost_id** out of the 757 MB settled file into a `set` each call. This is the
   idempotency guard (skip already-settled rows).
2. Iterates `_iter_jsonl(input_path)` (`:579`) — scans the **entire 721 MB** rejected
   file every call.
3. Per row: `gid = ghost_id(rec)` (`:86`, = `sha1(ts|market_id|reason)[:16]`); skip if
   in `settled_ids`; skip if no `market_id`; skip if market end is within
   `RESOLVED_BUFFER_SEC=90s` ("too_recent", `:594`); else `fetch_resolution()` via the
   Gamma API (`:305`, cached per `market_id` within a call); on resolve, append a
   settled record (`:678-690`) and feed the calibrator β (`:665-674`).

Why archiving naively breaks it: if old rows are moved out of the settled file,
`_load_settled_ids` no longer sees their ghost_ids → they look unsettled → re-fetched
from Gamma → re-appended. Loop reopens and the file regrows.

Consumers of the settled file (must keep working):
- `aggregate_ghost_buckets()` — `src/analysis/lane_thresholds.py:212` (per-lane ghost WR)
- `build_ghost_calibration_status()` — `ghost_calibration.py:697` (ops/dashboard counts)
- `_gl_load_ghosts(since)` — `src/dashboard/server.py:1902` (already time-filtered, 30d)

## Design

Three derived sidecar files under `data/calibration/`, all rebuildable:

### 1. `settled_index.txt` — idempotency index (~6 MB)
One `ghost_id` per line (16 hex chars). Replaces the 757 MB scan in `_load_settled_ids`.
- Loaded into a `set[str]` at loop start (load ~6 MB instead of 757 MB).
- Appended (one line) whenever a row is newly settled — alongside the existing settled
  jsonl append, in the same critical section.
- **Decouples idempotency from the settled jsonl**, so the settled jsonl can be
  archived/rotated freely.

### 2. `settle_cursor.json` — forward cursor on the append-only rejected file
```json
{
  "rejected_path": "rejected_candidates.jsonl",
  "rejected_inode": 1234567,
  "rejected_size": 721000000,
  "rejected_offset": 721000000,
  "updated_at": "2026-05-29T..."
}
```
- The rejected file is **append-only**, so a byte offset is a valid high-water mark for
  "rows already ingested." Each cycle: `open(rejected)`, `seek(rejected_offset)`, read
  only new bytes, advance offset to EOF. Turns the 721 MB rescan into "read what was
  appended since last cycle" (KB–MB).
- **Rotation/truncation guard:** before seeking, compare `st_ino` and `st_size`. If
  inode changed (file rotated) or size < stored offset (truncated/replaced), reset
  offset to 0 and re-ingest the current file from the top. Self-healing.

### 3. `settle_pending.jsonl` — retry queue for not-yet-resolvable rows
Critical subtlety: a rejected row may be **behind** the cursor but **not yet
settleable** — its market hasn't resolved (`too_recent`) or Gamma returned
unresolved/error (`unresolved_or_api`). A pure forward cursor would skip these forever.
So when ingesting new rejected rows, any row that does **not** settle on this pass is
written to `settle_pending.jsonl` (minimal fields: `ghost_id`, `ts`, `market_id`,
`market_end_ts`, `action`, `yes_price`, `no_price`, `reason`, lane/regime keys needed
by `normalize_ghost_metadata`). Each cycle the loop also retries the pending queue and
drops rows once settled. Pending stays small (only markets awaiting resolution, which
clear within hours).

### Per-cycle algorithm (replaces the current full scan)
```
load settled_index -> set            # ~6MB, or rebuild if missing (see fallback)
load cursor; verify inode/size guard # reset offset on rotation/truncation
load pending queue

# 1. ingest new rejected rows
seek rejected to cursor.offset
for rec in new lines:
    gid = ghost_id(rec)
    if gid in settled_index: continue          # already done
    attempt_settle(rec) -> settled | pending
advance cursor.offset to EOF; persist cursor (atomic)

# 2. retry the pending queue
for rec in pending:
    if gid in settled_index: drop; continue
    attempt_settle(rec) -> settled | keep_pending
rewrite pending queue (atomic)

# attempt_settle:
#   - too_recent (end within RESOLVED_BUFFER_SEC) -> pending
#   - fetch_resolution() None -> pending
#   - resolved -> append settled jsonl + append settled_index + feed β; settled
```
Semantics are **identical** to today (same `ghost_id`, same `fetch_resolution`, same
`compute_would_be`, same β feed) — only the iteration set shrinks.

### Fallback / self-heal (the robustness guarantee)
- **Missing/corrupt index or cursor** → rebuild index by one full scan of the settled
  jsonl (today's `_load_settled_ids`), reset cursor offset to 0, re-ingest rejected
  (already-settled rows are no-ops via the index). One slow cycle, then fast again.
- **Consistency check** on startup and every N cycles: `len(settled_index)` vs settled
  jsonl line count (cheap `wc`-style count, or trust within tolerance). On drift beyond
  tolerance → rebuild index.
- **Atomic writes** for index/cursor/pending: write temp + `os.replace`. The settled
  jsonl append stays append-only (`os.O_APPEND`) as today (`:681`).

## Archival (reclaim the existing 1.5 GB) — only after the above lands
With idempotency on `settled_index` and ingestion on the cursor, both JSONL files can
be sharded by era without touching the loop:

- Move rows older than a cutoff (e.g. `ts < 2026-05-27`, pre-current-config-era per the
  "time-filter to current era" memory rule) into compressed shards
  `data/calibration/archive/rejected_candidates_<YYYYMMDD>.jsonl.gz` and
  `..._settled_<YYYYMMDD>.jsonl.gz`.
- The live files keep only the recent tail. The loop is unaffected (index retains all
  ghost_ids; cursor tracks the live rejected file via inode-aware reset).
- **Consumers**: `aggregate_ghost_buckets` / `build_ghost_calibration_status` should
  (a) read the live tail by default (aligns with the documented current-era
  discipline and is what calibration decisions use), and (b) optionally glob the
  archive shards when a full-history view is explicitly requested. No data is lost —
  it's compressed and still readable.
- One-time migration script, dry-run first, with a `.bak`/checksum before deleting any
  original (the `.bak` patterns are already gitignored, `.gitignore:38-44`).

## Suggested implementation order (each independently shippable, each reversible)
1. **3a — `settled_index.txt`** (idempotency sidecar) + fallback rebuild. Biggest boot
   win (drops the 757 MB scan); unblocks settled-file archiving. Lowest risk.
2. **3b — `settle_cursor.json` + `settle_pending.jsonl`** (forward ingestion). Drops the
   721 MB rescan; makes the loop O(new). Inode guard + pending retry.
3. **3c — era archival/compaction** of existing data. Reclaims the 1.5 GB.

## Verification plan
- **Equivalence harness:** run the NEW checkpointed `settle_rejected_candidates` and the
  CURRENT full-scan version over the same frozen temp rejected/settled pair (stub
  `fetch_resolution`); assert the produced settled rows, ghost_ids, and β feeds are
  **identical sets**. This is the closed-loop proof.
- **Idempotency:** run the loop twice; second pass settles 0 new, appends 0 lines.
- **Pending correctness:** seed a row whose market resolves only on pass 2; assert it
  lands in pending on pass 1 and settles on pass 2 (no loss).
- **Rotation/truncation self-heal:** rotate the rejected file mid-test (new inode);
  assert cursor resets and no rows are skipped or double-settled.
- **Corrupt-checkpoint fallback:** delete/garble index + cursor; assert the loop rebuilds
  and converges to the same settled set as the full-scan baseline.
- **Archival round-trip:** archive an era, then assert `aggregate_ghost_buckets` over
  (live tail + archive shards) equals the pre-archive buckets; and the loop still
  settles new rows with 0 re-fetches of archived ghost_ids.
- Validate everything against the ghost log, never the broken backtester (per CLAUDE.md).

## Files this will touch (when implemented)
- `src/analysis/ghost_calibration.py` — `settle_rejected_candidates` (`:536`),
  `_load_settled_ids` (`:111`); new helpers: load/append index, load/verify cursor,
  load/rewrite pending, rebuild-on-corruption.
- `config/settings.yaml` — new `calibration.settle_checkpoint.{enabled, archive_cutoff_days,
   consistency_check_every_n}`; default `enabled: true` with full-scan fallback so it's
   safe.
- New: `scripts/archive_ghost_logs.py` (one-time era compaction, dry-run default).
- Consumers `aggregate_ghost_buckets` (`lane_thresholds.py:212`) /
  `build_ghost_calibration_status` (`ghost_calibration.py:697`) — optional archive-glob
  support (only if full-history view is needed).
