"""Rejected-candidate settlement and observability helpers.

The rejected-candidate tracker captures blocked entries that would otherwise
have been trades. This module closes that loop by:

1. Settling rejected candidates once their markets resolve.
2. Producing a compact runtime status snapshot for ops/dashboard surfaces.

The settled blocked-trade outcomes are intentionally kept separate from live lane
posteriors. They answer "what did the blocked trade do?" without polluting the
calibration stream for actually executed trades.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from src.analysis.lane_identity import build_lane_metadata, clean_lane_part, compose_lane_id

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"
DEFAULT_SETTLED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates_settled.jsonl"
# Derived idempotency sidecar (one ghost_id per line) — see step 3a of
# docs/GHOST_LOG_CHECKPOINT_SPEC.md. Lets the settle loop skip the full scan of
# the (large) settled jsonl when checking what is already settled. Pure cache:
# always rebuildable from DEFAULT_SETTLED_LOG; never the source of truth.
DEFAULT_SETTLED_INDEX = DEFAULT_CALIBRATION_DIR / "settled_index.txt"
DEFAULT_REGIME_LOG = DEFAULT_CALIBRATION_DIR / "market_regime.jsonl"
GAMMA_API = "https://gamma-api.polymarket.com"
RESOLVED_BUFFER_SEC = 90
REGIME_MATCH_MAX_AGE_SEC = 30 * 60
REGIME_FIELDS = (
    "price_regime",
    "polymarket_regime",
    "combined_regime",
    "regime_ts",
    "regime_match_age_sec",
    "regime_source",
)
REGIME_LABEL_FIELDS = ("price_regime", "polymarket_regime", "combined_regime")
REJECTED_COPY_FIELDS = (
    "btc_1h_regime",
    "context",
    # "probe_variants" intentionally dropped: not persisted by the writer and
    # read by no consumer. Its signal lives in the convergence_* scalars.
    "policy_version",
    "feature_hash",
    "side_source",
    "resolver_path",
    "primary_htf_bias",
    "alt_htf_bias",
    "btc_htf_bias",
    "lane_family",
    "entry_policy_snapshot",
    "lane_min_edge",
    "effective_min_edge",
    "raw_est_prob",
    "calibrated_est_prob",
    "gate_reason",
    "gate_stage",
    "convergence_score",
    "convergence_probe_count",
    "convergence_pass_count",
    "convergence_fail_count",
    "convergence_narrow_pass_count",
    "convergence_strong_pass_count",
    "edge_quality",
    "component_mean_quality",
    "edge_bucket",
    "entry_price_bucket",
    "correlation_bucket",
    "side_source_bucket",
    "regime_tag_bucket",
    "rsi_bucket",
    "atr_bucket",
)


def ghost_id(rec: Dict[str, Any]) -> str:
    key = f"{rec.get('ts','')}|{rec.get('market_id','')}|{rec.get('reason','')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_QGID_TS = re.compile(r'"ts"\s*:\s*"([^"]*)"')
_QGID_MID = re.compile(r'"market_id"\s*:\s*(?:"([^"]*)"|([^,}\s]+))')
_QGID_REASON = re.compile(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _quick_ghost_id(line: str) -> Optional[str]:
    """Compute ghost_id from a RAW jsonl line without a full json.loads.

    The settle loop re-reads the whole multi-hundred-MB rejected log every run
    just to skip the already-settled majority; full-parsing each fat nested row
    churns the allocator and ratchets RSS toward OOM. Extracting the three
    ghost_id inputs (ts|market_id|reason) by regex lets us skip settled rows
    without parsing them. SAFE: a mis-extraction yields an id that simply won't
    be in settled_ids, so it falls through to the authoritative full-parse path
    (only a sha1 collision could mis-skip — negligible). Returns None when the
    line can't be cheaply matched, forcing the full path.
    """
    mt = _QGID_TS.search(line)
    mm = _QGID_MID.search(line)
    mr = _QGID_REASON.search(line)
    if not (mt and mm and mr):
        return None
    ts = mt.group(1)
    mid = mm.group(1) if mm.group(1) is not None else mm.group(2)
    raw_reason = mr.group(1)
    try:
        reason = json.loads('"' + raw_reason + '"') if "\\" in raw_reason else raw_reason
    except json.JSONDecodeError:
        return None
    return hashlib.sha1(f"{ts}|{mid}|{reason}".encode("utf-8")).hexdigest()[:16]


def _iter_raw_lines(path: Path) -> Iterable[str]:
    """Yield stripped, non-empty raw lines (no json parse) for the pre-filter."""
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line
    except OSError as exc:
        logger.warning("rejected-candidate raw read failed (%s): %s", path, exc)


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        logger.warning("rejected_candidate_tracker read failed (%s): %s", path, exc)


def _load_settled_ids(path: Path) -> set[str]:
    return {
        str(obj["ghost_id"])
        for obj in _iter_jsonl(path)
        if obj.get("ghost_id")
    }


# ── Settled-index sidecar (step 3a) ───────────────────────────────────────────
# The index is a derived cache of the ghost_ids present in the settled record(s).
# It exists solely to avoid re-reading the multi-hundred-MB settled jsonl every
# cycle just to learn "what is already settled". The closed-loop guarantee is
# preserved because the index is ALWAYS reconciled to — and rebuildable from —
# the settled SOURCE OF TRUTH. After archival (step 3c) the source of truth is
# the live settled jsonl PLUS the compressed archive shards, so a rebuild scans
# both; this is why a shrunk live file (intentional archival) never causes the
# archived ghost_ids to be forgotten. If the index is missing or its meta is
# absent/garbled, we rebuild from that full source. Worst case == today's scan.


def _archived_settled_dir(settled_path: Path) -> Path:
    return settled_path.parent / "archive"


def _iter_archived_settled_ids(settled_path: Path) -> Iterable[str]:
    """Yield ghost_ids from compressed archive shards (step 3c output), if any.

    Archived rows are still settled — their ids MUST remain known so the loop
    never re-settles an archived ghost. Missing/unreadable shards are skipped
    (best-effort), but the live settled jsonl always remains authoritative.
    """
    import gzip

    archive_dir = _archived_settled_dir(settled_path)
    if not archive_dir.exists():
        return
    for shard in sorted(archive_dir.glob("*settled*archive*.jsonl.gz")):
        try:
            with gzip.open(shard, "rt", encoding="utf-8") as gz:
                for line in gz:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("ghost_id"):
                        yield str(obj["ghost_id"])
        except OSError as exc:
            logger.warning("settled_index: archive shard read failed (%s): %s", shard, exc)


def _settled_index_meta_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".meta.json")


def _read_settled_index_meta(index_path: Path) -> Optional[Dict[str, Any]]:
    meta_path = _settled_index_meta_path(index_path)
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_settled_index_meta(index_path: Path, settled_path: Path) -> None:
    """Record the settled file's identity (inode) + high-water size, so the next
    load can detect rotation/truncation and reconcile only the appended tail."""
    meta_path = _settled_index_meta_path(index_path)
    try:
        st = settled_path.stat() if settled_path.exists() else None
        meta = {
            "settled_inode": int(st.st_ino) if st else 0,
            "settled_offset": int(st.st_size) if st else 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = meta_path.with_name(meta_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(tmp, meta_path)  # atomic
    except OSError as exc:
        logger.warning("settled_index: meta write failed (%s): %s", meta_path, exc)


def _iter_ghost_ids_from_settled(path: Path, *, start_offset: int = 0) -> Iterable[str]:
    """Yield ghost_ids from the settled jsonl, optionally from a byte offset
    (the settled jsonl is append-only, so an offset is a valid resume point)."""
    if not path.exists():
        return
    try:
        with open(path, "rb") as fh:
            if start_offset:
                fh.seek(start_offset)
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("ghost_id"):
                    yield str(obj["ghost_id"])
    except OSError as exc:
        logger.warning("settled_index: tail read failed (%s): %s", path, exc)


def _write_settled_index(index_path: Path, gids: Iterable[str], settled_path: Path) -> None:
    """Atomically (re)write the full index file + refresh its meta."""
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = index_path.with_name(index_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for gid in gids:
                fh.write(str(gid) + "\n")
        os.replace(tmp, index_path)  # atomic
        _write_settled_index_meta(index_path, settled_path)
    except OSError as exc:
        logger.warning("settled_index: write failed (%s): %s", index_path, exc)


def _append_settled_index(index_path: Path, gids: Iterable[str]) -> None:
    """Append newly-settled ghost_ids to the index (lock-step with settled jsonl)."""
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, "a", encoding="utf-8") as fh:
            for gid in gids:
                fh.write(str(gid) + "\n")
    except OSError as exc:
        logger.warning("settled_index: append failed (%s): %s", index_path, exc)


def _rebuild_settled_index(settled_path: Path, index_path: Path) -> set[str]:
    # Source of truth = live settled jsonl UNION archive shards. Including the
    # archives is what guarantees the loop stays closed after step-3c archival:
    # a ghost that was settled-then-archived is still recognized as settled.
    ids = _load_settled_ids(settled_path)
    ids.update(_iter_archived_settled_ids(settled_path))
    _write_settled_index(index_path, ids, settled_path)
    return ids


def _load_settled_ids_indexed(settled_path: Path, index_path: Path) -> set[str]:
    """Return the set of already-settled ghost_ids via the sidecar index.

    Robust by construction — the returned set is always consistent with the
    settled source of truth (live jsonl + any archive shards), so settlement
    idempotency (the closed-loop guarantee) is preserved exactly. Falls back to
    a full rebuild whenever the cache cannot be trusted.
    """
    if not index_path.exists():
        return _rebuild_settled_index(settled_path, index_path)

    meta = _read_settled_index_meta(index_path)
    try:
        st = settled_path.stat() if settled_path.exists() else None
    except OSError:
        st = None

    # Cache is untrustworthy if: no meta, settled file gone, inode changed
    # (rotation), or recorded offset overruns current size (truncation/replace).
    if (
        meta is None
        or st is None
        or int(meta.get("settled_inode", -1)) != int(st.st_ino)
        or int(meta.get("settled_offset", -1)) > int(st.st_size)
    ):
        return _rebuild_settled_index(settled_path, index_path)

    # Load the cached ids.
    try:
        with open(index_path, encoding="utf-8") as fh:
            ids = {line.strip() for line in fh if line.strip()}
    except OSError as exc:
        logger.warning("settled_index: load failed (%s); rebuilding: %s", index_path, exc)
        return _rebuild_settled_index(settled_path, index_path)

    # Reconcile any rows appended to the settled jsonl since the index was last
    # updated (e.g. by an external backfill tool). Cheap: reads only the tail.
    offset = int(meta.get("settled_offset", 0))
    if int(st.st_size) > offset:
        new_ids = list(_iter_ghost_ids_from_settled(settled_path, start_offset=offset))
        missing = [g for g in new_ids if g not in ids]
        if missing:
            ids.update(missing)
            _append_settled_index(index_path, missing)
        _write_settled_index_meta(index_path, settled_path)

    return ids


def _load_rejected_by_id(path: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(path) or []:
        out[ghost_id(row)] = row
    return out


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_regime_snapshots(path: Path = DEFAULT_REGIME_LOG) -> List[Dict[str, Any]]:
    """Load market-regime snapshots sorted by timestamp."""
    snapshots: List[Dict[str, Any]] = []
    for row in _iter_jsonl(path) or []:
        ts = _parse_dt(row.get("ts"))
        if ts is None:
            continue
        snapshots.append({**row, "_parsed_ts": ts})
    snapshots.sort(key=lambda row: row["_parsed_ts"])
    return snapshots


def _nearest_regime_snapshot(
    target_ts: datetime,
    snapshots: Sequence[Dict[str, Any]],
    *,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
) -> Optional[Dict[str, Any]]:
    if not snapshots:
        return None

    lo = 0
    hi = len(snapshots)
    while lo < hi:
        mid = (lo + hi) // 2
        snap_ts = snapshots[mid].get("_parsed_ts")
        if not isinstance(snap_ts, datetime) or snap_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid

    best: Optional[Dict[str, Any]] = None
    best_age: Optional[float] = None
    for idx in (lo - 1, lo):
        if idx < 0 or idx >= len(snapshots):
            continue
        snap = snapshots[idx]
        snap_ts = snap.get("_parsed_ts")
        if not isinstance(snap_ts, datetime):
            continue
        age = abs((target_ts - snap_ts).total_seconds())
        if age <= max_age_sec and (best_age is None or age < best_age):
            best = snap
            best_age = age
    return best


def enrich_with_regime(
    row: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    *,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
    force: bool = False,
) -> Dict[str, Any]:
    """Return row with nearest market-regime labels for the ghost timestamp."""
    if not force and any(row.get(field) for field in REGIME_LABEL_FIELDS):
        return row

    out = dict(row)
    target_ts = _parse_dt(out.get("ts"))
    if target_ts is None:
        out.setdefault("regime_source", "missing_ts")
        return out

    snap = _nearest_regime_snapshot(target_ts, snapshots, max_age_sec=max_age_sec)
    if snap is None:
        out["regime_source"] = "unmatched"
        return out

    snap_ts = snap["_parsed_ts"]
    out["price_regime"] = snap.get("price_regime")
    out["polymarket_regime"] = snap.get("polymarket_regime")
    out["combined_regime"] = snap.get("combined_regime")
    out["regime_ts"] = snap.get("ts")
    out["regime_match_age_sec"] = round(abs((target_ts - snap_ts).total_seconds()), 3)
    out["regime_source"] = "market_regime"
    return out


def backfill_settled_regimes(
    *,
    input_path: Path = DEFAULT_SETTLED_LOG,
    output_path: Optional[Path] = None,
    regime_path: Path = DEFAULT_REGIME_LOG,
    rejected_path: Path = DEFAULT_REJECTED_LOG,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
    force: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Backfill regime labels onto settled ghost rows."""
    target_path = output_path or input_path
    summary = {
        "input_exists": input_path.exists(),
        "regime_exists": regime_path.exists(),
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
        "already_labelled": 0,
        "rejected_metadata_copied": 0,
        "written": 0,
    }
    if not input_path.exists():
        return summary

    snapshots = load_regime_snapshots(regime_path)
    rejected_lookup = _load_rejected_by_id(rejected_path) if rejected_path.exists() else {}
    rows: List[Dict[str, Any]] = []
    for row in _iter_jsonl(input_path) or []:
        summary["rows"] += 1
        was_labelled = any(row.get(field) for field in REGIME_LABEL_FIELDS)
        rejected_src = rejected_lookup.get(str(row.get("ghost_id") or ""))
        metadata_copied = False
        enriched_row = dict(row)
        if rejected_src:
            for field in REJECTED_COPY_FIELDS:
                if force or not enriched_row.get(field):
                    if rejected_src.get(field) is not None:
                        enriched_row[field] = rejected_src.get(field)
                        metadata_copied = True
        enriched_row = normalize_ghost_metadata(enriched_row)
        if metadata_copied:
            summary["rejected_metadata_copied"] += 1
        if was_labelled and not force and not metadata_copied:
            summary["already_labelled"] += 1
            rows.append(enriched_row)
            continue
        enriched = enrich_with_regime(
            enriched_row,
            snapshots,
            max_age_sec=max_age_sec,
            force=force,
        )
        if enriched.get("regime_source") == "market_regime":
            summary["matched"] += 1
        else:
            summary["unmatched"] += 1
        rows.append(enriched)

    if dry_run:
        return summary

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o644)
    try:
        for row in rows:
            clean = {k: v for k, v in row.items() if k != "_parsed_ts"}
            os.write(
                fd,
                (json.dumps(clean, separators=(",", ":")) + "\n").encode("utf-8"),
            )
            summary["written"] += 1
    finally:
        os.close(fd)
    os.replace(tmp_path, target_path)
    return summary


def fetch_resolution(
    market_id: str,
    cache: Dict[str, Optional[str]],
    *,
    timeout: float = 10.0,
) -> Optional[str]:
    """Return ``YES`` / ``NO`` / ``None`` for unresolved or unavailable."""
    if market_id in cache:
        return cache[market_id]
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=timeout)
        if resp.status_code != 200:
            cache[market_id] = None
            return None
        data = resp.json()
        if not data.get("closed", False):
            cache[market_id] = None
            return None
        resolution = data.get("resolution")
        if isinstance(resolution, str) and resolution.upper() in {"YES", "NO"}:
            cache[market_id] = resolution.upper()
            return cache[market_id]
        outcome_prices = data.get("outcomePrices")
        if outcome_prices:
            try:
                prices = (
                    json.loads(outcome_prices)
                    if isinstance(outcome_prices, str)
                    else outcome_prices
                )
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    if yes_price >= 0.99:
                        cache[market_id] = "YES"
                        return "YES"
                    if yes_price <= 0.01:
                        cache[market_id] = "NO"
                        return "NO"
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        cache[market_id] = None
        return None
    except requests.RequestException:
        cache[market_id] = None
        return None


def compute_would_be(
    *,
    action: str,
    yes_price: Optional[float],
    no_price: Optional[float],
    outcome: str,
) -> Dict[str, Any]:
    action = (action or "").upper()
    if action == "BUY_YES":
        entry = yes_price
        won = outcome == "YES"
    elif action == "BUY_NO":
        entry = no_price if no_price else ((1.0 - yes_price) if yes_price else None)
        won = outcome == "NO"
    else:
        return {
            "win": None,
            "realized_pct": None,
            "hypothetical_payout": None,
            "hypothetical_notional": None,
        }

    if entry is None or entry <= 0 or entry >= 1:
        return {
            "win": bool(won),
            "realized_pct": None,
            "hypothetical_payout": 1.0 if won else 0.0,
            "hypothetical_notional": None,
        }

    realized = (1.0 - entry) / entry if won else -1.0
    return {
        "win": bool(won),
        "realized_pct": round(realized, 6),
        "hypothetical_payout": 1.0 if won else 0.0,
        "hypothetical_notional": round(entry, 6),
    }


def _valid_lane_id(value: Any) -> str:
    lane_id = str(value or "").strip()
    return lane_id if len(lane_id.split("|")) >= 5 else ""


def _lane_id_from_context(rec: Dict[str, Any]) -> str:
    context = rec.get("context")
    if not isinstance(context, dict):
        return ""
    return _valid_lane_id(context.get("calibration_lane_id"))


def _reconstruct_live_lane_id(rec: Dict[str, Any], parts: Sequence[str]) -> str:
    context = rec.get("context")
    if not isinstance(context, dict):
        context = {}

    strategy = str(rec.get("strategy") or (parts[0] if len(parts) >= 1 else "")).strip()
    window = str(rec.get("window") or (parts[1] if len(parts) >= 2 else "")).strip()
    direction = str(parts[2] if len(parts) >= 3 else "").strip()
    if not strategy or not window or not direction:
        return ""

    primary_bias = str(
        rec.get("primary_htf_bias")
        or context.get("primary_htf_bias")
        or rec.get("htf_bias")
        or context.get("htf_bias")
        or ""
    ).strip()
    alt_bias = str(
        rec.get("alt_htf_bias")
        or context.get("alt_htf_bias")
        or ""
    ).strip()
    btc_bias = str(
        rec.get("btc_htf_bias")
        or rec.get("btc_1h_regime")
        or context.get("btc_1h_regime")
        or ""
    ).strip()
    if strategy != "bitcoin" and primary_bias and not alt_bias:
        alt_bias = primary_bias

    lane_meta = build_lane_metadata(
        strategy=strategy,
        window_size=window,
        direction=direction,
        side_source=rec.get("side_source") or context.get("side_source"),
        resolver_path=rec.get("resolver_path") or context.get("resolver_path"),
        ai_used=bool(context.get("ai_used")),
        reason=rec.get("reason"),
        signal_reason=context.get("signal_reason") or rec.get("reason"),
        htf_bias=(primary_bias if strategy == "bitcoin" else None),
        primary_htf_bias=(None if strategy == "bitcoin" else primary_bias),
        alt_htf_bias=(None if strategy == "bitcoin" else alt_bias),
        btc_1h_regime=(None if strategy == "bitcoin" else btc_bias),
    )
    lane_regime = str(lane_meta.get("lane_regime") or "").strip()
    lane_family = ""
    for candidate in (
        rec.get("lane_family"),
        context.get("lane_family"),
        context.get("entry_family"),
        parts[4] if len(parts) >= 5 and parts[4] != "rejected" else "",
    ):
        lane_family = clean_lane_part(candidate, default="")
        if lane_family:
            break
    if not lane_family:
        lane_family = str(lane_meta.get("entry_family") or "").strip()
    return compose_lane_id(
        strategy=strategy,
        window_size=window,
        lane_side=direction,
        lane_regime=lane_regime or "unclassified",
        entry_family=lane_family or "standard",
    )


def _biases_from_live_lane(lane_id: str) -> Dict[str, str]:
    parts = lane_id.split("|")
    if len(parts) < 4:
        return {}
    bits = [bit for bit in parts[3].split("__") if bit]
    if len(bits) >= 3:
        return {
            "primary_htf_bias": bits[0].upper(),
            "alt_htf_bias": bits[1].upper(),
            "btc_htf_bias": bits[2].upper(),
            "regime_tag_bucket": parts[3],
        }
    if bits:
        return {"primary_htf_bias": bits[0].upper(), "regime_tag_bucket": parts[3]}
    return {}


def normalize_ghost_metadata(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Add stable persistent metadata without changing the original ghost lane."""
    out = dict(rec)
    ghost_lane_id = _valid_lane_id(out.get("ghost_lane_id") or out.get("lane_id"))
    live_lane_id = _valid_lane_id(out.get("live_lane_id")) or _lane_id_from_context(out)
    if not live_lane_id:
        keys = _ghost_to_live_lane_keys(out)
        live_lane_id = keys[0] if keys else ""
    if ghost_lane_id:
        out["ghost_lane_id"] = ghost_lane_id
        if out.get("lane_id") in (None, ""):
            out["lane_id"] = ghost_lane_id
    if live_lane_id:
        out["live_lane_id"] = live_lane_id
        parts = live_lane_id.split("|")
        if len(parts) >= 5 and not out.get("lane_family"):
            out["lane_family"] = parts[4]
        for key, value in _biases_from_live_lane(live_lane_id).items():
            if out.get(key) in (None, ""):
                out[key] = value

    context = out.get("context")
    if isinstance(context, dict):
        for key in ("side_source", "resolver_path", "effective_min_edge", "raw_est_prob"):
            if out.get(key) in (None, "") and context.get(key) not in (None, ""):
                out[key] = context.get(key)
        if out.get("calibrated_est_prob") in (None, ""):
            out["calibrated_est_prob"] = (
                context.get("estimated_prob")
                if context.get("estimated_prob") not in (None, "")
                else context.get("calibrated_est_prob")
            )
    return out


def _ghost_to_live_lane_keys(rec: Dict[str, Any]) -> List[str]:
    """Map a rejected/ghost record's lane_id to the live lane_id key(s) that
    self-healing should update.

    Prefer the exact ``live_lane_id`` when the writer supplied one. Otherwise
    rebuild the live lane from the record's side-selection metadata so new
    family names like ``*_native`` and ``*_neutral_fallback_*`` survive ghost
    settlement instead of being collapsed into ``standard``.
    """
    live_lane_id = _valid_lane_id(rec.get("live_lane_id")) or _lane_id_from_context(rec)
    if live_lane_id:
        return [live_lane_id]

    lid = str(rec.get("lane_id") or "")
    parts = lid.split("|")
    if len(parts) < 3:
        return []
    live_lane_id = _valid_lane_id(_reconstruct_live_lane_id(rec, parts))
    if not live_lane_id:
        return []
    return [live_lane_id]


def settle_rejected_candidates(
    *,
    input_path: Path = DEFAULT_REJECTED_LOG,
    output_path: Path = DEFAULT_SETTLED_LOG,
    regime_path: Path = DEFAULT_REGIME_LOG,
    index_path: Path = DEFAULT_SETTLED_INDEX,
    use_index: bool = True,
    regime_max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    throttle_sec: float = 0.0,
    calibrator: Optional[Any] = None,
    ghost_weight: float = 0.5,
) -> Dict[str, Any]:
    """Settle any newly resolvable ghost candidates.

    Idempotent: previously settled rows are skipped via ``ghost_id``.

    When ``calibrator`` is supplied, each newly-settled record's would-have-been
    outcome is fed to ``calibrator.record_ghost(lane_id, win, weight=ghost_weight)``
    so β-vetoed lanes can self-heal from observation alone. Default weight is
    0.5x of a live trade — ghost outcomes lack slippage/fills, so they
    shouldn't dominate β 1:1 with real trades.
    """
    summary = {
        "input_exists": input_path.exists(),
        "already_settled": 0,
        "too_recent": 0,
        "no_market_id": 0,
        "unresolved_or_api": 0,
        "newly_settled": 0,
        "regime_matched": 0,
        "regime_unmatched": 0,
        "written": 0,
        "ghost_beta_updates": 0,
    }
    if not input_path.exists():
        return summary

    settled_ids = (
        _load_settled_ids_indexed(output_path, index_path)
        if use_index
        else _load_settled_ids(output_path)
    )
    regime_snapshots = load_regime_snapshots(regime_path)
    cache: Dict[str, Optional[str]] = {}
    settle_records: List[Dict[str, Any]] = []
    newly_settled_ids: List[str] = []
    ts_now = now or datetime.now(timezone.utc)

    for _line in _iter_raw_lines(input_path):
        # Cheap pre-filter: skip already-settled rows WITHOUT a full json.loads of
        # the fat nested row. Most rows are already settled, so this avoids ~95%
        # of the per-settle parse churn that was ratcheting RSS toward OOM.
        _qgid = _quick_ghost_id(_line)
        if _qgid is not None and _qgid in settled_ids:
            summary["already_settled"] += 1
            continue
        try:
            rec = json.loads(_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        gid = ghost_id(rec)
        if gid in settled_ids:
            summary["already_settled"] += 1
            continue

        market_id = str(rec.get("market_id") or "")
        if not market_id:
            summary["no_market_id"] += 1
            continue

        end_ts = str(rec.get("market_end_ts") or "")
        if end_ts:
            try:
                end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                if (ts_now - end_dt).total_seconds() < RESOLVED_BUFFER_SEC:
                    summary["too_recent"] += 1
                    continue
            except ValueError:
                pass

        outcome = fetch_resolution(market_id, cache)
        if throttle_sec > 0:
            time.sleep(throttle_sec)
        if outcome is None:
            summary["unresolved_or_api"] += 1
            continue

        wb = compute_would_be(
            action=str(rec.get("action") or ""),
            yes_price=(
                float(rec["yes_price"])
                if rec.get("yes_price") not in (None, "")
                else None
            ),
            no_price=(
                float(rec["no_price"])
                if rec.get("no_price") not in (None, "")
                else None
            ),
            outcome=outcome,
        )
        rejected_metadata = {
            field: rec.get(field)
            for field in REJECTED_COPY_FIELDS
            if rec.get(field) is not None
        }
        settled_rec = {
            "ghost_id": gid,
            "settled_at": ts_now.isoformat(),
            "outcome": outcome,
            **wb,
            "ts": rec.get("ts"),
            "lane_id": rec.get("lane_id"),
            "strategy": rec.get("strategy"),
            "window": rec.get("window"),
            "side": rec.get("side"),
            "action": rec.get("action"),
            "reason": rec.get("reason"),
            "market_id": market_id,
            "market_question": rec.get("market_question"),
            "yes_price": rec.get("yes_price"),
            "no_price": rec.get("no_price"),
            "est_prob_up": rec.get("est_prob_up"),
            "htf_bias": rec.get("htf_bias"),
            **rejected_metadata,
        }
        settled_rec = normalize_ghost_metadata(settled_rec)
        settled_rec = enrich_with_regime(
            settled_rec,
            regime_snapshots,
            max_age_sec=regime_max_age_sec,
        )
        if settled_rec.get("regime_source") == "market_regime":
            summary["regime_matched"] += 1
        else:
            summary["regime_unmatched"] += 1
        settle_records.append(settled_rec)
        newly_settled_ids.append(gid)
        summary["newly_settled"] += 1

        # Self-healing: feed ghost outcome into calibrator β at reduced weight,
        # translated to the LIVE lane_id key(s) so the veto check can actually
        # see the updates. Lets β-vetoed lanes climb back above the veto
        # threshold if their would-have-been WR recovers, without re-exposing
        # capital. Translation is heuristic (alt 4H assumed == alt 1H) — when
        # metadata is missing, the update is skipped.
        if calibrator is not None:
            try:
                win_val = wb.get("win") if isinstance(wb, dict) else None
                if isinstance(win_val, bool):
                    live_keys = _ghost_to_live_lane_keys(rec)
                    for live_lane_id in live_keys:
                        calibrator.record_ghost(
                            live_lane_id, win_val, weight=ghost_weight
                        )
                        summary["ghost_beta_updates"] += 1
            except Exception as _gpe:  # noqa: BLE001 — telemetry must not block settle
                logger.warning("ghost calibrator update skipped: %s", _gpe)

    if not dry_run and settle_records:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(output_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                for rec in settle_records:
                    os.write(
                        fd,
                        (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"),
                    )
                    summary["written"] += 1
            finally:
                os.close(fd)
            # Keep the idempotency index in lock-step with the settled jsonl so
            # the next cycle does not re-settle these rows, and advance the tail
            # offset to the new EOF. Best-effort: a failure here only costs a
            # future rebuild (the index self-heals), never correctness.
            if use_index and newly_settled_ids:
                _append_settled_index(index_path, newly_settled_ids)
                _write_settled_index_meta(index_path, output_path)
        except OSError as exc:
            logger.warning("rejected_candidate_tracker append failed (%s): %s", output_path, exc)

    return summary


def build_ghost_calibration_status(
    *,
    rejected_path: Path = DEFAULT_REJECTED_LOG,
    settled_path: Path = DEFAULT_SETTLED_LOG,
) -> Dict[str, Any]:
    """Return a compact status block for OPS_JSON / dashboard consumers."""
    # Stream both logs once each instead of materializing ~GB of dicts via
    # list() and looping the settled records three separate times. The rejected
    # log is only needed for its row count, so it is never materialized. Output
    # is byte-for-byte identical to the previous multi-pass implementation.
    total_rejected = sum(1 for _ in _iter_jsonl(rejected_path))

    total_settled = 0
    wins = 0
    losses = 0
    by_reason_action: Dict[str, Dict[str, int]] = {}
    last_settled_at = ""
    regime_stats: Dict[str, Dict[str, Any]] = {}
    conv_buckets = {"high": 0, "medium": 0, "low": 0, "none": 0}

    for rec in _iter_jsonl(settled_path):
        total_settled += 1
        win = rec.get("win")
        if win is True:
            wins += 1
        elif win is False:
            losses += 1

        # reason|action breakdown
        reason = str(rec.get("reason") or "?")
        action = str(rec.get("action") or "?")
        key = f"{reason}|{action}"
        bucket = by_reason_action.setdefault(key, {"wins": 0, "losses": 0, "n": 0})
        if win is True:
            bucket["wins"] += 1
        elif win is False:
            bucket["losses"] += 1
        bucket["n"] += 1

        settled_at = str(rec.get("settled_at") or "")
        if settled_at and settled_at > last_settled_at:
            last_settled_at = settled_at

        # BTC 1H regime breakdown
        regime = str(rec.get("btc_1h_regime") or "UNKNOWN")
        rbucket = regime_stats.setdefault(regime, {"wins": 0, "losses": 0, "n": 0})
        rbucket["n"] += 1
        if win is True:
            rbucket["wins"] += 1
        elif win is False:
            rbucket["losses"] += 1

        # Convergence score breakdown
        score = rec.get("convergence_score")
        if score is None:
            conv_buckets["none"] += 1
        elif score >= 0.6:
            conv_buckets["high"] += 1
        elif score >= 0.4:
            conv_buckets["medium"] += 1
        else:
            conv_buckets["low"] += 1

    unresolved = max(0, total_rejected - total_settled)

    ordered = sorted(
        by_reason_action.items(),
        key=lambda kv: kv[1]["n"],
        reverse=True,
    )[:10]
    total_decided = wins + losses

    regime_breakdown = {}
    for regime, stats in sorted(regime_stats.items()):
        decided = stats["wins"] + stats["losses"]
        regime_breakdown[regime] = {
            "n": stats["n"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": round(stats["wins"] / decided, 4) if decided else None,
        }

    return {
        "rejected_log_exists": rejected_path.exists(),
        "settled_log_exists": settled_path.exists(),
        "total_rejected": total_rejected,
        "total_settled": total_settled,
        "unresolved": unresolved,
        "wins": wins,
        "losses": losses,
        "settled_win_rate": round(wins / total_decided, 4) if total_decided else None,
        "last_settled_at": last_settled_at or None,
        "top_reason_action": {
            key: value for key, value in ordered
        },
        "btc_1h_regime_breakdown": regime_breakdown,
        "convergence_score_breakdown": conv_buckets,
    }
