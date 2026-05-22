#!/usr/bin/env python3
"""Reconstruct regime and convergence metadata on settled ghost candidates.

This is the historical close-the-loop pass for older settled ghost rows that
were written before the live ghost logger carried BTC 1H regime and convergence
telemetry. It intentionally marks reconstructed values with source fields so
analysis can separate live-captured telemetry from backfilled labels.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.analysis.btc_1h_regime import classify_btc_1h_sma_regime  # noqa: E402
from src.analysis.ghost_calibration import (  # noqa: E402
    DEFAULT_REJECTED_LOG,
    DEFAULT_SETTLED_LOG,
    ghost_id,
)
from src.analysis.rejected_candidate_log import compute_convergence_telemetry  # noqa: E402
from src.backtest.ohlcv_loader import OHLCVLoader  # noqa: E402

DEFAULT_REPORT_DIR = REPO_ROOT / "data" / "reports"
DEFAULT_RANGE_BAND_PCT = 0.0012

REASON_PRIOR_SCORES = {
    "liquidity": 0.35,
    "oracle_basis_block": 0.35,
    "oracle": 0.35,
    "entry_window": 0.35,
    "no_btc_catalyst": 0.40,
    "btc_catalyst_5m": 0.40,
    "corr_floor_5m": 0.42,
    "signal_strength_5m": 0.45,
    "iql_15m": 0.45,
    "low_corr_suppressed": 0.42,
    "lane_min_edge": 0.50,
    "edge_cap": 0.50,
    "ai_veto": 0.50,
}


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
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


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def load_rejected_by_id(path: Path) -> Dict[str, Dict[str, Any]]:
    return {ghost_id(row): row for row in iter_jsonl(path) or []}


def build_completed_btc_1h_frame(
    btc_15m: pd.DataFrame,
    *,
    range_band_pct: float = DEFAULT_RANGE_BAND_PCT,
) -> pd.DataFrame:
    """Return completed 1H BTC candles labeled by close/end time."""
    if btc_15m.empty:
        return pd.DataFrame()

    df = btc_15m.copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time")
    df["hour_start"] = df["open_time"].dt.floor("h")
    hourly = (
        df.groupby("hour_start", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            bars=("close", "size"),
        )
        .sort_values("hour_start")
        .reset_index(drop=True)
    )
    hourly = hourly[hourly["bars"] >= 4].copy()
    hourly["hour_end"] = hourly["hour_start"] + pd.Timedelta(hours=1)
    hourly["sma20"] = hourly["close"].rolling(20, min_periods=20).mean()
    hourly["btc_1h_regime"] = [
        (
            classify_btc_1h_sma_regime(float(close), float(sma), range_band_pct)
            if pd.notna(sma)
            else None
        )
        for close, sma in zip(hourly["close"], hourly["sma20"])
    ]
    return hourly.dropna(subset=["sma20", "btc_1h_regime"]).reset_index(drop=True)


class BtcRegimeLookup:
    def __init__(self, hourly: pd.DataFrame):
        self.hourly = hourly
        self.ends: List[datetime] = []
        if not hourly.empty:
            self.ends = [
                ts.to_pydatetime().astimezone(timezone.utc)
                for ts in pd.to_datetime(hourly["hour_end"], utc=True)
            ]

    def lookup(self, ts: Optional[datetime]) -> Optional[Dict[str, Any]]:
        if ts is None or not self.ends:
            return None
        idx = bisect_right(self.ends, ts) - 1
        if idx < 0:
            return None
        row = self.hourly.iloc[idx]
        return {
            "btc_1h_regime": str(row["btc_1h_regime"]),
            "btc_1h_regime_price": round(float(row["close"]), 6),
            "btc_1h_regime_sma20": round(float(row["sma20"]), 6),
            "btc_1h_regime_ts": self.ends[idx].isoformat(),
            "btc_1h_regime_source": "ohlcv_15m_resample",
            "btc_1h_regime_reconstructed": True,
        }


def load_btc_regime_lookup(
    rows: List[Dict[str, Any]],
    *,
    range_band_pct: float = DEFAULT_RANGE_BAND_PCT,
    no_cache: bool = False,
) -> BtcRegimeLookup:
    parsed = [ts for ts in (parse_ts(row.get("ts")) for row in rows) if ts is not None]
    if not parsed:
        return BtcRegimeLookup(pd.DataFrame())
    start = (min(parsed) - timedelta(days=3)).date().isoformat()
    end = max(parsed).date().isoformat()
    btc_15m = OHLCVLoader(no_cache=no_cache).load("BTCUSDT", "15m", start, end)
    hourly = build_completed_btc_1h_frame(btc_15m, range_band_pct=range_band_pct)
    return BtcRegimeLookup(hourly)


def _copy_missing_metadata(row: Dict[str, Any], source: Optional[Dict[str, Any]]) -> None:
    if not source:
        return
    for field in (
        "context",
        "probe_variants",
        "policy_version",
        "feature_hash",
        "effective_min_edge",
        "raw_est_prob",
        "est_prob_up",
        "yes_price",
        "edge",
        "confidence",
    ):
        if row.get(field) is None and source.get(field) is not None:
            row[field] = source[field]


def _derive_edge(row: Dict[str, Any]) -> Optional[float]:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    direct = as_float(row.get("edge"))
    if direct is None:
        direct = as_float(context.get("edge"))
    if direct is not None:
        return direct

    est = as_float(row.get("est_prob_up"))
    if est is None:
        est = as_float(row.get("raw_est_prob"))
    if est is None:
        est = as_float(context.get("est_prob_up"))
    price = as_float(row.get("yes_price"))
    if price is None:
        price = as_float(context.get("yes_price"))
    if est is None or price is None:
        return None
    action = str(row.get("action") or "").upper()
    if action == "BUY_NO":
        return price - est
    return est - price


def _derive_min_edge(row: Dict[str, Any]) -> Optional[float]:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    for value in (
        row.get("effective_min_edge"),
        context.get("effective_min_edge"),
        row.get("min_edge"),
        context.get("min_edge"),
    ):
        out = as_float(value)
        if out is not None:
            return out
    return None


def _reason_prior(row: Dict[str, Any]) -> float:
    reason = str(row.get("reason") or "").lower()
    for key, score in REASON_PRIOR_SCORES.items():
        if key in reason:
            return score
    return 0.50


def reconstruct_convergence(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return convergence telemetry with explicit reconstruction source."""
    probe_variants = row.get("probe_variants")
    if not isinstance(probe_variants, list):
        probe_variants = []
    edge = _derive_edge(row)
    min_edge = _derive_min_edge(row)
    telemetry = compute_convergence_telemetry(
        probe_variants=probe_variants,
        edge=edge,
        effective_min_edge=min_edge,
    )
    score = as_float(telemetry.get("convergence_score"))
    if score is not None:
        source_parts = []
        if probe_variants:
            source_parts.append("probe")
        if edge is not None and min_edge is not None:
            source_parts.append("edge")
        return {
            **telemetry,
            "convergence_score": round(score, 6),
            "convergence_source": "_".join(source_parts) or "computed",
            "convergence_reconstructed": True,
        }

    prior = _reason_prior(row)
    return {
        "convergence_score": round(prior, 6),
        "convergence_probe_count": 0,
        "convergence_pass_count": 0,
        "convergence_fail_count": 0,
        "convergence_narrow_pass_count": 0,
        "convergence_strong_pass_count": 0,
        "edge_quality": None,
        "component_mean_quality": None,
        "convergence_source": "reason_prior",
        "convergence_reconstructed": True,
    }


def _fallback_regime(row: Dict[str, Any]) -> Dict[str, Any]:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    htf_bias = str(context.get("htf_bias") or row.get("htf_bias") or "").upper()
    if "BEAR" in htf_bias or "DOWN" in htf_bias:
        regime = "BEAR"
        source = "htf_bias_fallback"
    elif "BULL" in htf_bias or "UP" in htf_bias:
        regime = "BULL"
        source = "htf_bias_fallback"
    else:
        regime = "RANGE"
        source = "neutral_fallback"
    return {
        "btc_1h_regime": regime,
        "btc_1h_regime_source": source,
        "btc_1h_regime_reconstructed": True,
    }


def reconstruct_rows(
    rows: List[Dict[str, Any]],
    *,
    rejected_by_id: Dict[str, Dict[str, Any]],
    regime_lookup: BtcRegimeLookup,
    force: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {
        "rows": len(rows),
        "btc_1h_regime_added": 0,
        "convergence_added": 0,
        "copied_rejected_metadata": 0,
        "regime_sources": Counter(),
        "convergence_sources": Counter(),
    }

    for original in rows:
        row = dict(original)
        source = rejected_by_id.get(str(row.get("ghost_id") or "")) or rejected_by_id.get(
            ghost_id(row)
        )
        before_keys = set(row.keys())
        _copy_missing_metadata(row, source)
        if set(row.keys()) != before_keys:
            summary["copied_rejected_metadata"] += 1

        if force or not row.get("btc_1h_regime"):
            regime = regime_lookup.lookup(parse_ts(row.get("ts"))) or _fallback_regime(row)
            row.update(regime)
            summary["btc_1h_regime_added"] += 1
        summary["regime_sources"][str(row.get("btc_1h_regime_source") or "existing")] += 1

        if force or row.get("convergence_score") is None:
            row.update(reconstruct_convergence(row))
            summary["convergence_added"] += 1
        summary["convergence_sources"][str(row.get("convergence_source") or "existing")] += 1
        out.append(row)

    summary["regime_sources"] = dict(summary["regime_sources"])
    summary["convergence_sources"] = dict(summary["convergence_sources"])
    summary["missing_btc_1h_regime_after"] = sum(1 for row in out if not row.get("btc_1h_regime"))
    summary["missing_convergence_after"] = sum(
        1 for row in out if row.get("convergence_score") is None
    )
    summary["btc_1h_regime_counts"] = dict(Counter(str(row.get("btc_1h_regime")) for row in out))
    return out, summary


def write_report(summary: Dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"ghost_metadata_reconstruction_{stamp}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct btc_1h_regime and convergence_score on settled ghosts."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_SETTLED_LOG)
    parser.add_argument("--output", type=Path, default=None, help="Defaults to in-place rewrite.")
    parser.add_argument("--rejected-log", type=Path, default=DEFAULT_REJECTED_LOG)
    parser.add_argument("--range-band-pct", type=float, default=DEFAULT_RANGE_BAND_PCT)
    parser.add_argument("--force", action="store_true", help="Recompute existing metadata too.")
    parser.add_argument("--no-cache", action="store_true", help="Force fresh OHLCV fetch.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = list(iter_jsonl(args.input) or [])
    rejected_by_id = load_rejected_by_id(args.rejected_log)
    regime_lookup = load_btc_regime_lookup(
        rows,
        range_band_pct=args.range_band_pct,
        no_cache=args.no_cache,
    )
    reconstructed, summary = reconstruct_rows(
        rows,
        rejected_by_id=rejected_by_id,
        regime_lookup=regime_lookup,
        force=args.force,
    )
    summary.update(
        {
            "input": str(args.input),
            "output": str(args.output or args.input),
            "dry_run": bool(args.dry_run),
            "btc_hourly_rows": len(regime_lookup.hourly),
        }
    )

    if not args.dry_run:
        output = args.output or args.input
        if output == args.input and not args.no_backup and args.input.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = args.input.with_suffix(args.input.suffix + f".bak-{stamp}")
            shutil.copy2(args.input, backup)
            summary["backup"] = str(backup)
        write_jsonl(output, reconstructed)
        summary["report"] = str(write_report(summary, args.report_dir))

    if args.json:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(f"rows: {summary['rows']}")
        print(f"btc_1h_regime_added: {summary['btc_1h_regime_added']}")
        print(f"convergence_added: {summary['convergence_added']}")
        print(f"missing_btc_1h_regime_after: {summary['missing_btc_1h_regime_after']}")
        print(f"missing_convergence_after: {summary['missing_convergence_after']}")
        print(f"regime_sources: {summary['regime_sources']}")
        print(f"convergence_sources: {summary['convergence_sources']}")
        if summary.get("backup"):
            print(f"backup: {summary['backup']}")
        if summary.get("report"):
            print(f"report: {summary['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
