#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.underperformance_audit import load_backtest_action_summary


@dataclass(frozen=True)
class XRPRoundTrip:
    session_id: str
    trade_id: str
    entry_ts: str
    exit_ts: str
    action: str
    window_size: str
    htf_bias: str
    btc_1h_regime: str
    yes_price_entry: Optional[float]
    edge_entry: float
    entry_price: float
    current_price: float
    exit_reason: str
    pnl: float
    hour_pt: Optional[int]
    corr_1h: Optional[float]
    rsi: Optional[float]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _load_xrp_round_trips(paper_root: Path, sessions: Iterable[str]) -> List[XRPRoundTrip]:
    rows: List[XRPRoundTrip] = []
    for session_id in sessions:
        entries_path = paper_root / session_id / "entries.jsonl"
        if not entries_path.exists():
            continue
        entry_by_tid: Dict[str, Dict[str, Any]] = {}
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = str(payload.get("trade_id") or "")
            if not tid:
                continue
            evt = str(payload.get("event") or "")
            if evt == "ENTRY":
                entry_by_tid[tid] = payload
            elif evt == "EXIT" and payload.get("strategy") == "xrp_macro":
                ent = entry_by_tid.get(tid, {})
                ex_extra = payload.get("extra") or {}
                en_extra = ent.get("extra") or {}
                merged = {**en_extra, **ex_extra}

                entry_price = _safe_float(ent.get("entry_price"))
                current_price = _safe_float(payload.get("current_price"))
                if entry_price > 0 and abs(entry_price + current_price - 1.0) < 0.02:
                    # Phantom binary row.
                    continue

                rows.append(
                    XRPRoundTrip(
                        session_id=session_id,
                        trade_id=tid,
                        entry_ts=str(ent.get("timestamp") or ""),
                        exit_ts=str(payload.get("timestamp") or ""),
                        action=str(payload.get("action") or ""),
                        window_size=str(merged.get("window_size") or ""),
                        htf_bias=str(merged.get("htf_bias") or ""),
                        btc_1h_regime=str(merged.get("btc_1h_regime") or ""),
                        yes_price_entry=(
                            float(merged["yes_price"])
                            if merged.get("yes_price") is not None
                            else None
                        ),
                        edge_entry=_safe_float(merged.get("entry_edge", merged.get("edge"))),
                        entry_price=entry_price,
                        current_price=current_price,
                        exit_reason=str(payload.get("reason") or ""),
                        pnl=_safe_float(payload.get("pnl")),
                        hour_pt=_safe_int(merged.get("hour_pt")),
                        corr_1h=(
                            float(merged["corr_1h"])
                            if merged.get("corr_1h") is not None
                            else None
                        ),
                        rsi=(
                            float(merged["rsi"])
                            if merged.get("rsi") is not None
                            else None
                        ),
                    )
                )
    return rows


def _load_xrp_buy_no_skips(paper_root: Path, sessions: Iterable[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for session_id in sessions:
        entries_path = paper_root / session_id / "entries.jsonl"
        if not entries_path.exists():
            continue
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("event") != "BUY_NO_SKIP" or payload.get("strategy") != "xrp_macro":
                continue
            extra = payload.get("extra") or {}
            out.append(
                {
                    "session_id": session_id,
                    "skip_reason": str(payload.get("reason") or extra.get("skip_reason") or "unknown"),
                    "edge": _safe_float(extra.get("edge")),
                    "effective_min_edge": _safe_float(extra.get("effective_min_edge")),
                    "yes_price": extra.get("yes_price"),
                    "window_size": str(extra.get("window_size") or ""),
                    "htf_bias": str(extra.get("htf_bias") or ""),
                    "alt_1h_trend": str(extra.get("alt_1h_trend") or ""),
                }
            )
    return out


def _summarize(rows: List[XRPRoundTrip]) -> Dict[str, Any]:
    n = len(rows)
    pnl = sum(r.pnl for r in rows)
    wins = sum(1 for r in rows if r.pnl > 0)
    losses = sum(1 for r in rows if r.pnl < 0)
    return {
        "trades": n,
        "net_pnl": round(pnl, 4),
        "win_rate": round(wins / n, 4) if n else 0.0,
        "wins": wins,
        "losses": losses,
        "avg_pnl": round(pnl / n, 4) if n else 0.0,
    }


def _exit_bucket_stats(rows: List[XRPRoundTrip]) -> Dict[str, Any]:
    bucket: Dict[str, List[XRPRoundTrip]] = defaultdict(list)
    for r in rows:
        bucket[r.exit_reason].append(r)
    out: Dict[str, Any] = {}
    for reason, vals in sorted(bucket.items()):
        pnls = [x.pnl for x in vals]
        out[reason] = {
            "trades": len(vals),
            "net_pnl": round(sum(pnls), 4),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(vals), 4) if vals else 0.0,
            "avg_win": round(mean([p for p in pnls if p > 0]), 4) if any(p > 0 for p in pnls) else None,
            "avg_loss": round(mean([p for p in pnls if p < 0]), 4) if any(p < 0 for p in pnls) else None,
            "loss_share_of_negative_pnl": 0.0,  # filled below
        }
    neg_total = abs(sum(r.pnl for r in rows if r.pnl < 0))
    if neg_total > 0:
        for reason, vals in bucket.items():
            loss = abs(sum(v.pnl for v in vals if v.pnl < 0))
            out[reason]["loss_share_of_negative_pnl"] = round(loss / neg_total, 4)
    return out


def _edge_quality(rows: List[XRPRoundTrip]) -> Dict[str, Any]:
    def edge_bucket(e: float) -> str:
        if e < 0.08:
            return "<0.08"
        if e < 0.10:
            return "0.08-0.10"
        if e < 0.12:
            return "0.10-0.12"
        return ">=0.12"

    groups: Dict[str, List[XRPRoundTrip]] = defaultdict(list)
    for r in rows:
        groups[f"edge:{edge_bucket(r.edge_entry)}"].append(r)
        groups[f"window:{r.window_size or 'unknown'}"].append(r)
        groups[f"action:{r.action}"].append(r)
        groups[f"hour_pt:{r.hour_pt if r.hour_pt is not None else 'unknown'}"].append(r)
        groups[f"regime:{r.btc_1h_regime or 'unknown'}"].append(r)

    out: Dict[str, Any] = {}
    for k, vals in sorted(groups.items()):
        pnls = [v.pnl for v in vals]
        out[k] = {
            "trades": len(vals),
            "net_pnl": round(sum(pnls), 4),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(vals), 4) if vals else 0.0,
            "avg_edge_entry": round(mean(v.edge_entry for v in vals), 4),
            "avg_yes_price_entry": round(mean(v.yes_price_entry for v in vals if v.yes_price_entry is not None), 4)
            if any(v.yes_price_entry is not None for v in vals)
            else None,
        }
    return out


def _load_settings(settings_path: Path) -> Dict[str, Any]:
    return yaml.safe_load(settings_path.read_text(encoding="utf-8"))


def _parity_check(settings: Dict[str, Any], backtest_xrp: Dict[str, Any]) -> Dict[str, Any]:
    s = (((settings.get("strategies") or {}).get("xrp_macro")) or {})
    b = (settings.get("backtest") or {})
    t = ((settings.get("trading") or {}).get("exit_rules") or {})

    checks = {
        "min_edge_15m_live_vs_backtest": {
            "live": _safe_float(s.get("min_edge")),
            "backtest": _safe_float(b.get("min_edge_xrp_15m")),
        },
        "min_edge_5m_live_vs_backtest": {
            "live": _safe_float(s.get("min_edge_5m")),
            "backtest": _safe_float(b.get("min_edge_xrp_5m")),
        },
        "buy_no_extra_floor_live": {
            "live_min_edge_buy_no": _safe_float(s.get("min_edge_buy_no")),
            "note": "Backtest report does not encode live-only BUY_NO extra floor directly.",
        },
        "entry_windows_live": {
            "entry_window_15m_min": _safe_float(s.get("entry_window_15m_min")),
            "entry_window_15m_max": _safe_float(s.get("entry_window_15m_max")),
            "entry_window_5m_min": _safe_float(s.get("entry_window_5m_min")),
            "entry_window_5m_max": _safe_float(s.get("entry_window_5m_max")),
        },
        "regime_gate_live": {
            "enabled": bool(((s.get("btc_1h_regime_gates") or {}).get("enabled"))),
            "min_edge_mult": ((s.get("btc_1h_regime_gates") or {}).get("min_edge_mult") or {}),
            "size_mult": ((s.get("btc_1h_regime_gates") or {}).get("size_mult") or {}),
        },
        "updown_exit_rule_live_global": {
            "updown_stop_cents": _safe_float(t.get("updown_stop_cents")),
            "updown_exit_window_mins": _safe_float(t.get("updown_exit_window_mins")),
            "updown_max_hold_mins": _safe_float(t.get("updown_max_hold_mins")),
            "note": "Global updown exits apply to xrp_macro unless code adds strategy-specific overrides.",
        },
        "backtest_control": {
            "report_file": backtest_xrp.get("file"),
            "trades": backtest_xrp.get("trades_count"),
            "net_pnl": backtest_xrp.get("net_pnl"),
            "buy_no_net_pnl": _safe_float(((backtest_xrp.get("by_action") or {}).get("BUY_NO") or {}).get("net_pnl")),
            "buy_yes_net_pnl": _safe_float(((backtest_xrp.get("by_action") or {}).get("BUY_YES") or {}).get("net_pnl")),
        },
    }
    return checks


def _ranked_candidates(
    recent_rows: List[XRPRoundTrip],
    baseline_rows: List[XRPRoundTrip],
    parity: Dict[str, Any],
    backtest_xrp: Dict[str, Any],
) -> List[Dict[str, Any]]:
    recent_neg = abs(sum(r.pnl for r in recent_rows if r.pnl < 0))
    ts_loss = abs(sum(r.pnl for r in recent_rows if r.exit_reason == "updown_time_stop" and r.pnl < 0))
    high_price_losses = abs(
        sum(r.pnl for r in recent_rows if r.action == "BUY_YES" and r.entry_price >= 0.50 and r.pnl < 0)
    )
    recent_buy_no_n = sum(1 for r in recent_rows if r.action == "BUY_NO")
    baseline_buy_no_n = sum(1 for r in baseline_rows if r.action == "BUY_NO")
    backtest_buy_no = _safe_float(((backtest_xrp.get("by_action") or {}).get("BUY_NO") or {}).get("net_pnl"))
    backtest_buy_yes = _safe_float(((backtest_xrp.get("by_action") or {}).get("BUY_YES") or {}).get("net_pnl"))

    cands: List[Dict[str, Any]] = []
    cands.append(
        {
            "id": "A_buy_no_admission_relief",
            "priority": 1,
            "why_now": (
                f"Recent BUY_NO executions={recent_buy_no_n} vs baseline={baseline_buy_no_n}; "
                f"backtest BUY_NO net={backtest_buy_no:+.2f}, BUY_YES net={backtest_buy_yes:+.2f}."
            ),
            "proposed_change": {
                "xrp_macro.enforce_alt_1h_alignment": False,
                "xrp_macro.min_edge_buy_no": 0.08,
            },
            "predicted_effect": "Increase BUY_NO share and reduce one-sided BUY_YES drawdowns in adverse micro-regimes.",
            "success_metric": "Within next 20 XRP closes: BUY_NO share >= 10% and XRP net PnL non-negative.",
            "failure_trigger": "After 20 XRP closes, BUY_NO net PnL < -$3 or total XRP net PnL worsens vs pre-change.",
            "rollback_rule": "Revert enforce_alt_1h_alignment=true and min_edge_buy_no=0.10.",
        }
    )
    cands.append(
        {
            "id": "B_xrp_specific_time_stop_soften",
            "priority": 2,
            "why_now": (
                f"Recent updown_time_stop BUY_YES losses={ts_loss:.2f}, "
                f"{(ts_loss / recent_neg):.1%} of recent negative PnL." if recent_neg > 0 else "No recent negative PnL."
            ),
            "proposed_change": {
                "xrp_exit_rule_override": {
                    "updown_stop_cents": 0.04,
                    "updown_exit_window_mins": 1.50,
                }
            },
            "predicted_effect": "Reduce premature adverse exits and lower time-stop loss concentration.",
            "success_metric": "updown_time_stop loss share of negative XRP PnL < 30% over next 20 XRP closes.",
            "failure_trigger": "Two consecutive >$4 realized losses immediately after softening stop.",
            "rollback_rule": "Restore current global updown_stop_cents/updown_exit_window_mins values.",
        }
    )
    cands.append(
        {
            "id": "C_tighten_high_price_buy_yes_15m",
            "priority": 3,
            "why_now": f"High-price (>=0.50) BUY_YES recent losses totaled {high_price_losses:.2f}.",
            "proposed_change": {
                "xrp_macro.entry_price_max": 0.55,
                "xrp_macro.entry_window_15m_max": 15.0,
            },
            "predicted_effect": "Reduce tail losses from expensive YES entries in 15m windows.",
            "success_metric": "No single XRP BUY_YES loss worse than -$4.00 across next 20 XRP closes.",
            "failure_trigger": "Trade count collapse (>40% drop) with no PnL improvement over same horizon.",
            "rollback_rule": "Restore prior entry_price_max and entry_window_15m_max values.",
        }
    )
    return cands


def _select_bad_sessions(paper_root: Path, all_sessions: List[str], baseline: str, recent_n: int) -> List[str]:
    scored: List[tuple[str, float, int]] = []
    for session_id in all_sessions:
        if session_id == baseline:
            continue
        rows = _load_xrp_round_trips(paper_root, [session_id])
        if not rows:
            continue
        pnl = sum(r.pnl for r in rows)
        scored.append((session_id, pnl, len(rows)))
    scored.sort(key=lambda x: (x[1], x[2]))  # most negative first
    return [sid for sid, _p, _n in scored[:recent_n]]


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    meta = report["meta"]
    lines.append("# XRP Forensic Audit")
    lines.append("")
    lines.append("## Scope")
    lines.append(f"- Baseline session: `{meta['baseline_session']}`")
    lines.append(f"- Recent bad sessions: `{', '.join(meta['recent_bad_sessions'])}`")
    lines.append(f"- Generated at (UTC): `{meta['generated_at_utc']}`")
    lines.append("")
    lines.append("## Performance Summary")
    for lbl in ("baseline", "recent_bad"):
        s = report["summary"][lbl]
        lines.append(
            f"- **{lbl.replace('_', ' ').title()}**: {s['trades']} trades, "
            f"net {s['net_pnl']:+.2f}, WR {s['win_rate']:.1%}, wins/losses {s['wins']}/{s['losses']}"
        )
    lines.append("")
    lines.append("## Exit-Path Contribution (Recent Bad Sessions)")
    lines.append("| exit_reason | trades | net_pnl | WR | loss_share_of_negative_pnl |")
    lines.append("|---|---:|---:|---:|---:|")
    for reason, row in sorted(report["recent_exit_buckets"].items(), key=lambda kv: kv[1]["net_pnl"]):
        lines.append(
            f"| {reason} | {row['trades']} | {row['net_pnl']:+.2f} | {row['win_rate']:.1%} | {row['loss_share_of_negative_pnl']:.1%} |"
        )
    lines.append("")
    lines.append("## BUY_NO Suppression Diagnostics")
    bnd = report["buy_no_diagnostics"]
    lines.append(f"- Executed BUY_NO (baseline): `{bnd['executed_buy_no_baseline']}`")
    lines.append(f"- Executed BUY_NO (recent bad): `{bnd['executed_buy_no_recent']}`")
    lines.append(f"- BUY_NO skip telemetry rows (xrp only): `{bnd['xrp_buy_no_skip_events']}`")
    lines.append(f"- Backtest BUY_NO net PnL: `{bnd['backtest_buy_no_net_pnl']:+.2f}`")
    lines.append(f"- Backtest BUY_YES net PnL: `{bnd['backtest_buy_yes_net_pnl']:+.2f}`")
    lines.append("")
    lines.append("## Edge Quality (Recent Bad Sessions)")
    for k, v in sorted(report["recent_edge_quality"].items()):
        if not (k.startswith("edge:") or k.startswith("window:") or k.startswith("action:")):
            continue
        lines.append(
            f"- `{k}` -> n={v['trades']}, net={v['net_pnl']:+.2f}, WR={v['win_rate']:.1%}, avg_edge={v['avg_edge_entry']:.3f}"
        )
    lines.append("")
    lines.append("## Live-vs-Backtest Parity")
    lines.append("```json")
    lines.append(json.dumps(report["parity_checks"], indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Ranked Remediation Candidates")
    for c in report["ranked_candidates"]:
        lines.append(f"### {c['id']} (priority {c['priority']})")
        lines.append(f"- Why now: {c['why_now']}")
        lines.append(f"- Proposed change: `{json.dumps(c['proposed_change'])}`")
        lines.append(f"- Predicted effect: {c['predicted_effect']}")
        lines.append(f"- Success metric: {c['success_metric']}")
        lines.append(f"- Failure trigger: {c['failure_trigger']}")
        lines.append(f"- Rollback rule: {c['rollback_rule']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    default_paper_root = repo_root / "data" / "paper_trades"
    default_settings = repo_root / "config" / "settings.yaml"
    default_reports = repo_root / "data" / "backtest" / "reports"
    default_out_dir = repo_root / "docs" / "session_reports"

    ap = argparse.ArgumentParser(description="Run full XRP forensic audit.")
    ap.add_argument("--paper-root", type=Path, default=default_paper_root)
    ap.add_argument("--settings", type=Path, default=default_settings)
    ap.add_argument("--reports-dir", type=Path, default=default_reports)
    ap.add_argument("--out-dir", type=Path, default=default_out_dir)
    ap.add_argument("--baseline-session", default="test_20260504_034719")
    ap.add_argument(
        "--recent-bad-sessions",
        default="",
        help="Comma-separated session IDs. If omitted, auto-select most negative XRP sessions.",
    )
    ap.add_argument("--recent-bad-count", type=int, default=3)
    ap.add_argument("--label", default="xrp_forensic_audit")
    args = ap.parse_args()

    all_sessions = [p.name for p in args.paper_root.iterdir() if p.is_dir()]
    if args.recent_bad_sessions.strip():
        recent_bad = [s.strip() for s in args.recent_bad_sessions.split(",") if s.strip()]
    else:
        recent_bad = _select_bad_sessions(
            args.paper_root, all_sessions, baseline=args.baseline_session, recent_n=args.recent_bad_count
        )

    baseline_rows = _load_xrp_round_trips(args.paper_root, [args.baseline_session])
    recent_rows = _load_xrp_round_trips(args.paper_root, recent_bad)
    recent_skips = _load_xrp_buy_no_skips(args.paper_root, recent_bad)

    backtests = load_backtest_action_summary(args.reports_dir)
    xrp_backtest = backtests.get("xrp_macro", {})

    settings = _load_settings(args.settings)
    parity = _parity_check(settings, xrp_backtest)

    report = {
        "meta": {
            "baseline_session": args.baseline_session,
            "recent_bad_sessions": recent_bad,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "summary": {
            "baseline": _summarize(baseline_rows),
            "recent_bad": _summarize(recent_rows),
        },
        "baseline_dataset": [r.__dict__ for r in baseline_rows],
        "recent_dataset": [r.__dict__ for r in recent_rows],
        "recent_exit_buckets": _exit_bucket_stats(recent_rows),
        "buy_no_diagnostics": {
            "executed_buy_no_baseline": sum(1 for r in baseline_rows if r.action == "BUY_NO"),
            "executed_buy_no_recent": sum(1 for r in recent_rows if r.action == "BUY_NO"),
            "xrp_buy_no_skip_events": len(recent_skips),
            "xrp_buy_no_skip_reason_counts": dict(Counter(r["skip_reason"] for r in recent_skips)),
            "backtest_buy_no_net_pnl": _safe_float(((xrp_backtest.get("by_action") or {}).get("BUY_NO") or {}).get("net_pnl")),
            "backtest_buy_yes_net_pnl": _safe_float(((xrp_backtest.get("by_action") or {}).get("BUY_YES") or {}).get("net_pnl")),
        },
        "recent_edge_quality": _edge_quality(recent_rows),
        "parity_checks": parity,
        "ranked_candidates": _ranked_candidates(recent_rows, baseline_rows, parity, xrp_backtest),
        "acceptance_checks": {
            "reproducibility_note": "Deterministic from local session files and settings at run time.",
            "accounting_check": {
                "recent_net_from_rows": round(sum(r.pnl for r in recent_rows), 6),
                "recent_net_from_exit_buckets": round(
                    sum(v["net_pnl"] for v in _exit_bucket_stats(recent_rows).values()), 6
                ),
                "baseline_net_from_rows": round(sum(r.pnl for r in baseline_rows), 6),
            },
        },
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = args.out_dir / f"{args.label}_{stamp}.json"
    md_path = args.out_dir / f"{args.label}_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
