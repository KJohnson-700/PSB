"""AI-decision settler — the accuracy counterfactual the decision layer never had.

The AI decision layer was enabled as a live entry gate before anyone measured
whether its verdicts are actually *right*. We have months of AI verdicts logged
(data/logs/ai_pipeline/marginal_analysis.jsonl: market_id, recommendation,
confidence_score, estimated_probability per call) but they were never scored
against the real Polymarket outcome. So we could not answer the only question
that justifies gating on the model:

    "When the AI gives a directional call (or vetoes one), is it more right than
     a coin flip — and is its P(YES) calibrated?"

This module fills that gap, mirroring taken_exit_settler:

  - reads AI verdicts from marginal_analysis.jsonl (the model's own probability +
    recommendation), looks up each market's final resolution via the proven
    ResolutionTracker Gamma fetch (shared resolution cache),
  - scores Brier (vs P(YES)), directional hit-rate, calibration-by-confidence,
    and per-strategy breakdowns,
  - separately scores VETO QUALITY from rejected_candidate_observer.jsonl: when
    the AI said HOLD, would the quant's intended trade have *won*? If AI-HOLD
    rows would have won >50%, the model's vetoes are blocking winners (harmful);
    <50% means the vetoes are protective.

Resolved outcomes never change, so they are cached and re-runs are cheap.

Run:  python -m src.analysis.ai_decision_settler [--since YYYY-MM-DD] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.execution.resolution_tracker import ResolutionTracker

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_LOG_DIR = REPO_ROOT / "data" / "logs" / "ai_pipeline"
MARGINAL_LOG = AI_LOG_DIR / "marginal_analysis.jsonl"
OBSERVER_LOG = AI_LOG_DIR / "rejected_candidate_observer.jsonl"
# Real, live gate verdicts (approved + rejected) written by the strategies'
# _log_decision_layer. THIS is the file that measures the actual decision layer,
# as opposed to the shadow marginal log. Empty until a dry_run runs with the gate on.
DECISION_LOG = AI_LOG_DIR / "decision_layer.jsonl"

CALIB_DIR = REPO_ROOT / "data" / "calibration"
SETTLED_PATH = CALIB_DIR / "ai_decisions_settled.jsonl"
# Shared with taken_exit_settler so resolution fetches are reused across settlers.
RESOLUTION_CACHE_PATH = CALIB_DIR / "_market_resolution_cache.json"

CONFIDENCE_BUCKETS: Tuple[Tuple[float, float, str], ...] = (
    (0.0, 0.50, "<0.50"),
    (0.50, 0.60, "0.50-0.60"),
    (0.60, 0.70, "0.60-0.70"),
    (0.70, 0.80, "0.70-0.80"),
    (0.80, 1.01, ">=0.80"),
)


def _iter_jsonl(path: Path, since: Optional[str]) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    since_dt = _parse_since(since)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_dt is not None:
                ts = _parse_ts(row.get("ts_utc"))
                if ts is not None and ts < since_dt:
                    continue
            yield row


def _parse_since(since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    try:
        return datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _confidence_bucket(conf: Optional[float]) -> str:
    if conf is None:
        return "unknown"
    for lo, hi, label in CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return label
    return "unknown"


def _window_from_lane(lane_id: Any) -> str:
    """lane_id like 'hype_macro|15m|up|...' -> '15m'."""
    if not isinstance(lane_id, str):
        return ""
    parts = lane_id.split("|")
    return parts[1] if len(parts) > 1 else ""


def _resolve_outcomes(
    market_ids: Iterable[str], batch_size: int = 50
) -> Dict[str, str]:
    """Return {market_id: 'YES'/'NO'} for resolved markets, using a shared cache."""
    cache = _load_cache()
    needed = sorted({str(m) for m in market_ids if str(m) not in cache})
    if needed:
        tracker = ResolutionTracker()
        logger.info("fetching %d unresolved markets", len(needed))
        for i in range(0, len(needed), batch_size):
            batch = needed[i : i + batch_size]
            fetched = tracker._fetch_resolutions(batch)  # noqa: SLF001 — proven Gamma lookup
            for mid, res in fetched.items():
                if res.get("resolved") and res.get("outcome_won"):
                    cache[str(mid)] = {"outcome_won": res["outcome_won"]}
        _save_cache(cache)
    return {
        mid: rec["outcome_won"]
        for mid, rec in cache.items()
        if rec.get("outcome_won") in ("YES", "NO")
    }


def _load_cache() -> Dict[str, Dict[str, Any]]:
    if RESOLUTION_CACHE_PATH.exists():
        try:
            return json.loads(RESOLUTION_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESOLUTION_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(RESOLUTION_CACHE_PATH)


def settle(since: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    """Score logged AI verdicts against real resolutions; write JSONL + return summary."""
    verdicts: List[Dict[str, Any]] = []
    for row in _iter_jsonl(MARGINAL_LOG, since):
        mid = row.get("market_id")
        rec = str(row.get("recommendation") or "").strip().upper()
        est = _as_float(row.get("estimated_probability"))
        if not mid or not rec:
            continue
        verdicts.append(
            {
                "market_id": str(mid),
                "ts_utc": row.get("ts_utc"),
                "strategy": row.get("strategy_hint"),
                "window": _window_from_lane(row.get("lane_id")),
                "lane_id": row.get("lane_id"),
                "recommendation": rec,
                "confidence": _as_float(row.get("confidence_score")),
                "est_prob_yes": est,
            }
        )
        if limit is not None and len(verdicts) >= limit:
            break

    # Veto-quality rows: AI HOLD vs the quant action it blocked.
    vetoes: List[Dict[str, Any]] = []
    for row in _iter_jsonl(OBSERVER_LOG, since):
        mid = row.get("market_id")
        direct = str(row.get("direct_recommendation") or "").strip().upper()
        quant = str(row.get("quant_action") or "").strip().upper()
        if not mid or direct != "HOLD" or quant not in ("BUY_YES", "BUY_NO"):
            continue
        vetoes.append(
            {
                "market_id": str(mid),
                "ts_utc": row.get("ts_utc"),
                "strategy": row.get("strategy_hint"),
                "quant_action": quant,
                "ai_confidence": _as_float(row.get("direct_confidence")),
            }
        )

    # Real live gate decisions (the actual decision layer, not shadow).
    decisions: List[Dict[str, Any]] = []
    for row in _iter_jsonl(DECISION_LOG, since):
        mid = row.get("market_id")
        quant = str(row.get("quant_action") or "").strip().upper()
        if not mid or quant not in ("BUY_YES", "BUY_NO"):
            continue
        decisions.append(
            {
                "market_id": str(mid),
                "ts_utc": row.get("ts_utc"),
                "strategy": row.get("strategy"),
                "window": row.get("window"),
                "quant_action": quant,
                "approved": row.get("approved"),
                "fail_open": row.get("fail_open"),
                "reason": row.get("reason"),
            }
        )

    all_ids = (
        [v["market_id"] for v in verdicts]
        + [v["market_id"] for v in vetoes]
        + [d["market_id"] for d in decisions]
    )
    outcomes = _resolve_outcomes(all_ids)

    settled: List[Dict[str, Any]] = []
    n_unresolved = 0
    for v in verdicts:
        won = outcomes.get(v["market_id"])
        if won is None:
            n_unresolved += 1
            continue
        yes_won = 1 if won == "YES" else 0
        rec = v["recommendation"]
        rec_correct: Optional[bool] = None
        if rec == "BUY_YES":
            rec_correct = won == "YES"
        elif rec == "BUY_NO":
            rec_correct = won == "NO"
        est = v["est_prob_yes"]
        brier = (est - yes_won) ** 2 if est is not None else None
        dir_correct: Optional[bool] = None
        if est is not None and est != 0.5:
            dir_correct = (est > 0.5) == (yes_won == 1)
        settled.append(
            {
                **v,
                "outcome_won": won,
                "yes_won": yes_won,
                "rec_correct": rec_correct,
                "brier": round(brier, 4) if brier is not None else None,
                "est_dir_correct": dir_correct,
            }
        )

    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTLED_PATH, "w", encoding="utf-8") as fh:
        for rec in settled:
            fh.write(json.dumps(rec) + "\n")

    veto_settled = []
    for v in vetoes:
        won = outcomes.get(v["market_id"])
        if won is None:
            continue
        quant_would_win = (v["quant_action"] == "BUY_YES" and won == "YES") or (
            v["quant_action"] == "BUY_NO" and won == "NO"
        )
        veto_settled.append({**v, "outcome_won": won, "quant_would_win": quant_would_win})

    decision_settled = []
    for d in decisions:
        won = outcomes.get(d["market_id"])
        if won is None:
            continue
        quant_would_win = (d["quant_action"] == "BUY_YES" and won == "YES") or (
            d["quant_action"] == "BUY_NO" and won == "NO"
        )
        decision_settled.append({**d, "outcome_won": won, "quant_would_win": quant_would_win})

    summary = _summarize(settled, veto_settled)
    summary["real_decisions"] = _summarize_real_decisions(decision_settled)
    summary["n_verdicts"] = len(verdicts)
    summary["n_settled"] = len(settled)
    summary["n_unresolved"] = n_unresolved
    summary["n_vetoes_settled"] = len(veto_settled)
    summary["n_decisions_settled"] = len(decision_settled)
    return summary


def _summarize_real_decisions(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score the actual live gate: were APPROVED entries right, were REJECTED ones losers?"""
    approved = [r for r in rows if r.get("approved") is True and not r.get("fail_open")]
    rejected = [r for r in rows if r.get("approved") is False and not r.get("fail_open")]
    fail_open = [r for r in rows if r.get("fail_open")]
    appr_win = sum(1 for r in approved if r["quant_would_win"])
    rej_win = sum(1 for r in rejected if r["quant_would_win"])
    return {
        "n": len(rows),
        "approved_n": len(approved),
        "approved_win_pct": _rate(appr_win, len(approved)),
        "rejected_n": len(rejected),
        # high => the gate rejected winners (harmful); low => rejected losers (good)
        "rejected_would_win_pct": _rate(rej_win, len(rejected)),
        "fail_open_n": len(fail_open),
        "gate_value": (
            "no decisions yet — run a dry_run session with the gate on"
            if not rows
            else "GOOD — approves winners, rejects losers"
            if _rate(appr_win, len(approved)) > _rate(rej_win, max(1, len(rejected)))
            else "questionable — rejected set wins as often as approved"
        ),
    }


def _rate(num: int, den: int) -> float:
    return round(num / den * 100, 1) if den else 0.0


def _summarize(
    settled: List[Dict[str, Any]], vetoes: List[Dict[str, Any]]
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # Overall calibration / directional accuracy (rows with an est_prob).
    briers = [r["brier"] for r in settled if r["brier"] is not None]
    dir_rows = [r for r in settled if r["est_dir_correct"] is not None]
    out["overall"] = {
        "n": len(settled),
        "brier": round(sum(briers) / len(briers), 4) if briers else None,
        "brier_baseline_0.5": 0.25,
        "est_dir_acc_pct": _rate(
            sum(1 for r in dir_rows if r["est_dir_correct"]), len(dir_rows)
        ),
        "yes_base_rate_pct": _rate(sum(r["yes_won"] for r in settled), len(settled)),
    }

    # Directional recommendation accuracy (BUY_YES / BUY_NO only).
    by_rec: Dict[str, Counter] = defaultdict(Counter)
    for r in settled:
        c = by_rec[r["recommendation"]]
        c["n"] += 1
        if r["rec_correct"] is True:
            c["correct"] += 1
    out["by_recommendation"] = {
        rec: {"n": c["n"], "hit_pct": _rate(c["correct"], c["n"]) if rec in ("BUY_YES", "BUY_NO") else None}
        for rec, c in sorted(by_rec.items())
    }

    # Accuracy by AI confidence bucket — validates whether min_confidence is meaningful.
    by_conf: Dict[str, Counter] = defaultdict(Counter)
    for r in dir_rows:
        b = _confidence_bucket(r["confidence"])
        by_conf[b]["n"] += 1
        if r["est_dir_correct"]:
            by_conf[b]["correct"] += 1
    out["by_confidence"] = {
        b: {"n": by_conf[b]["n"], "dir_acc_pct": _rate(by_conf[b]["correct"], by_conf[b]["n"])}
        for b in sorted(by_conf)
    }

    # Per-strategy directional accuracy.
    by_strat: Dict[str, Counter] = defaultdict(Counter)
    for r in dir_rows:
        s = str(r.get("strategy") or "?")
        by_strat[s]["n"] += 1
        if r["est_dir_correct"]:
            by_strat[s]["correct"] += 1
    out["by_strategy"] = {
        s: {"n": by_strat[s]["n"], "dir_acc_pct": _rate(by_strat[s]["correct"], by_strat[s]["n"])}
        for s in sorted(by_strat)
    }

    # Veto quality: would the quant trade the AI vetoed (HOLD) have won?
    if vetoes:
        win = sum(1 for v in vetoes if v["quant_would_win"])
        out["veto_quality"] = {
            "n": len(vetoes),
            "quant_would_win_pct": _rate(win, len(vetoes)),
            "verdict": (
                "HARMFUL — vetoes blocked winners"
                if _rate(win, len(vetoes)) > 50.0
                else "protective — vetoes blocked losers"
            ),
        }
    else:
        out["veto_quality"] = {"n": 0}
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="only verdicts on/after this UTC date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="cap verdicts (debug)")
    args = ap.parse_args()

    s = settle(since=args.since, limit=args.limit)
    print("\n=== AI decision settlement ===")
    print(f"verdicts logged:        {s.pop('n_verdicts')}")
    print(f"settled (resolved):     {s.pop('n_settled')}")
    print(f"unresolved (skipped):   {s.pop('n_unresolved')}")
    print(f"vetoes settled:         {s.pop('n_vetoes_settled')}")
    print(f"real gate decisions:    {s.pop('n_decisions_settled')}")
    print(f"output: {SETTLED_PATH}\n")

    rd = s.pop("real_decisions", {"n": 0})
    print("=== REAL DECISION LAYER (live gate, not shadow) ===")
    if rd["n"] == 0:
        print("  no settled gate decisions yet — run a dry_run session with decision_layer on,")
        print("  then re-run this settler to score the ACTUAL gate. -> " + str(rd.get("gate_value")))
    else:
        print(f"  approved entries: n={rd['approved_n']}  won={rd['approved_win_pct']}%")
        print(f"  rejected entries: n={rd['rejected_n']}  would-have-won={rd['rejected_would_win_pct']}%")
        print(f"  fail-open (took quant, AI down/slow): n={rd['fail_open_n']}")
        print(f"  -> {rd['gate_value']}")
    print()

    ov = s["overall"]
    print(f"OVERALL  n={ov['n']}  Brier={ov['brier']} (baseline 0.25)  "
          f"est-dir-acc={ov['est_dir_acc_pct']}%  YES base rate={ov['yes_base_rate_pct']}%")
    print("\nby recommendation:")
    for rec, d in s["by_recommendation"].items():
        print(f"  {rec:9s} n={d['n']:5d}  hit={d['hit_pct']}%")
    print("\nby AI confidence (directional accuracy):")
    for b, d in s["by_confidence"].items():
        print(f"  {b:11s} n={d['n']:5d}  dir-acc={d['dir_acc_pct']}%")
    print("\nby strategy (directional accuracy):")
    for st, d in s["by_strategy"].items():
        print(f"  {st:12s} n={d['n']:5d}  dir-acc={d['dir_acc_pct']}%")
    vq = s["veto_quality"]
    if vq["n"]:
        print(f"\nVETO QUALITY  n={vq['n']}  quant-would-win={vq['quant_would_win_pct']}%  -> {vq['verdict']}")
    else:
        print("\nVETO QUALITY  no settle-able HOLD vetoes found")


if __name__ == "__main__":
    main()
