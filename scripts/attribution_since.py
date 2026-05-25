#!/usr/bin/env python3
"""
Multi-session paper journal attribution: join ENTRY+EXIT across session dirs, filter by
exit time / session mtime, aggregate strategy × action × exit_reason + cheap tag columns.

Writes docs/session_reports/attribution_since_<label>.{md,json}.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.execution.trade_journal import is_phantom_exit_row


def _parse_exit_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _first_line_timestamp(path: Path) -> datetime | None:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            return _parse_exit_ts(str(o.get("timestamp") or ""))
    return None


def _tag_side_src(sig: str, reason_entry: str) -> str:
    for blob in (sig, reason_entry):
        m = re.search(r"side_src=([^\s|]+)", blob)
        if m:
            return m.group(1).strip()
    return ""


def _is_phantom_binary(ex: dict, ent: dict) -> bool:
    return is_phantom_exit_row(ex)


def closed_rows_from_jsonl(
    path: Path,
    exit_since: datetime | None,
    session_id: str,
) -> list[dict[str, Any]]:
    since_aware: datetime | None = None
    if exit_since is not None:
        since_aware = (
            exit_since
            if exit_since.tzinfo
            else exit_since.replace(tzinfo=timezone.utc)
        )

    open_entries: dict[str, dict] = {}
    exits: list[dict] = []

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = e.get("event", "")
            tid = e.get("trade_id") or ""
            if et == "ENTRY" and tid:
                open_entries[tid] = e
            elif et == "EXIT" and tid:
                exits.append(e)

    closed: list[dict[str, Any]] = []
    for ex in exits:
        tid = ex.get("trade_id", "")
        ent = open_entries.get(tid, {})
        ee = ent.get("extra") or {}
        xo = ex.get("extra") or {}
        merged = {**ee, **xo}

        if _is_phantom_binary(ex, ent):
            continue

        exit_dt = _parse_exit_ts(str(ex.get("timestamp") or ""))
        if since_aware is not None:
            if exit_dt is None:
                continue
            ed = exit_dt if exit_dt.tzinfo else exit_dt.replace(tzinfo=timezone.utc)
            if ed < since_aware:
                continue

        pnl = float(ex.get("pnl") or 0)
        sig = str(merged.get("signal_reason") or ee.get("signal_reason") or "")
        reason_entry = str(ent.get("reason", ""))
        reason_ex = str(ex.get("reason", ""))

        lag_mag = merged.get("lag_magnitude")
        if lag_mag is None and "macro_leg=" in reason_entry:
            try:
                part = reason_entry.split("macro_leg=")[1].split()[0]
                lag_mag = float(part)
            except (IndexError, ValueError):
                lag_mag = None

        counter_trend_btc = (
            "counter_trend=btc_4h_hist_declining" in sig
            or "counter_trend=btc_4h_hist_declining" in reason_entry
        )
        btc_5m_against = "BTC5m against" in sig or "BTC5m against" in reason_entry
        bypass_5m = (
            "bypass_5m" in sig
            or "bypass_5m" in reason_entry
            or "bypass_5m" in reason_ex
        )

        closed.append(
            {
                "session_id": session_id,
                "trade_id": tid,
                "strategy": ex.get("strategy", ""),
                "action": ex.get("action", ""),
                "pnl": pnl,
                "exit_reason": reason_ex,
                "exit_ts": str(ex.get("timestamp") or ""),
                "window_size": merged.get("window_size"),
                "btc_1h_regime": merged.get("btc_1h_regime"),
                "side_src": _tag_side_src(sig, reason_entry),
                "counter_trend_btc": counter_trend_btc,
                "btc_5m_against_or_sig": btc_5m_against,
                "bypass_5m_impulse": bypass_5m,
                "lag_magnitude": lag_mag,
            }
        )
    return closed


def _discover_sessions(
    paper_root: Path,
    session_prefix: str | None,
    after_mtime_utc_date: str | None,
    explicit_sessions: list[str] | None,
) -> list[Path]:
    if explicit_sessions:
        out = []
        for name in explicit_sessions:
            p = paper_root / name
            if (p / "entries.jsonl").is_file():
                out.append(p)
        return sorted(out, key=lambda x: x.name)

    cutoff: float | None = None
    if after_mtime_utc_date:
        d = datetime.strptime(after_mtime_utc_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        cutoff = d.timestamp()

    sessions: list[Path] = []
    for d in sorted(paper_root.iterdir()):
        if not d.is_dir():
            continue
        if session_prefix and not d.name.startswith(session_prefix):
            continue
        ent = d / "entries.jsonl"
        if not ent.is_file():
            continue
        if cutoff is not None:
            try:
                if d.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
        sessions.append(d)
    return sessions


def _exit_stratify(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Hermes-style bucket counts + TP share over primary exit reasons (excludes 'other')."""

    def bucket(reason: str) -> str:
        if reason == "take_profit":
            return "take_profit"
        if reason == "updown_time_stop":
            return "updown_time_stop"
        if reason == "updown_expired":
            return "updown_expired"
        if reason == "RESOLVED:YES (real)":
            return "RESOLVED:YES (real)"
        if reason == "RESOLVED:NO (real)":
            return "RESOLVED:NO (real)"
        return "other"

    keys = (
        "take_profit",
        "updown_time_stop",
        "RESOLVED:YES (real)",
        "RESOLVED:NO (real)",
        "updown_expired",
        "other",
    )

    def hermes_den(ct: dict[str, int]) -> int:
        return sum(ct.get(x, 0) for x in keys[:-1])  # exclude 'other' from denominator

    def rollup(ct: dict[str, int]) -> dict[str, Any]:
        h = {k: int(ct.get(k, 0)) for k in keys}
        den = hermes_den(h)
        tp = h["take_profit"]
        return {
            **h,
            "hermes_bucket_n": den,
            "tp_share_of_hermes_buckets": round(tp / den, 4) if den else None,
        }

    overall: dict[str, int] = defaultdict(int)
    by_strat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_strat_win: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for r in rows:
        b = bucket(str(r.get("exit_reason") or ""))
        overall[b] += 1
        st = str(r.get("strategy") or "—")
        by_strat[st][b] += 1
        ws = r.get("window_size")
        ws_key = str(ws) if ws is not None else "unknown"
        by_strat_win[f"{st}::{ws_key}"][b] += 1

    return {
        "overall": rollup(dict(overall)),
        "by_strategy": {
            k: rollup(dict(v)) for k, v in sorted(by_strat.items(), key=lambda x: x[0])
        },
        "by_strategy_window": {
            k: rollup(dict(v))
            for k, v in sorted(by_strat_win.items(), key=lambda x: x[0])
        },
    }


def _agg_key(rows: list[dict], key_fn) -> dict[str, Any]:
    buckets: dict[Any, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0, "pnls": []}
    )
    for r in rows:
        k = key_fn(r)
        b = buckets[k]
        b["n"] += 1
        b["pnl"] += r["pnl"]
        b["pnls"].append(r["pnl"])
        if r["pnl"] > 0:
            b["wins"] += 1
    out: dict[str, Any] = {}
    for k, b in sorted(buckets.items(), key=lambda x: (str(type(x[0])), str(x[0]))):
        n = b["n"]
        out[str(k)] = {
            "n": n,
            "wins": b["wins"],
            "win_rate": round(b["wins"] / n, 4) if n else 0.0,
            "pnl": round(b["pnl"], 4),
            "avg_pnl": round(b["pnl"] / n, 4) if n else 0.0,
        }
    return out


def _render_md(payload: dict[str, Any]) -> str:
    meta = payload.get("meta", {})
    n = meta.get("closed_trades", 0)
    lines = [
        "# Attribution since — paper journal",
        "",
        f"- **Label:** `{meta.get('label', '')}`",
        f"- **Closed trades:** {n}",
        f"- **Sessions included:** {len(meta.get('sessions_included', []))}",
        f"- **Filters:** `{json.dumps(meta.get('filters', {}))}`",
        "",
    ]
    if n < 20:
        lines.append(
            "> **Guardrail:** Total n is small; the table shows *where dollars went* but "
            "does not prove which YAML knob caused it. Use staged rollbacks to isolate.\n"
        )

    sac = payload.get("by_strategy_action_exit", {})
    # Sort by pnl ascending for loss leaders
    ranked = sorted(
        sac.items(),
        key=lambda kv: kv[1].get("pnl", 0.0),
    )
    lines.append("## Top loss buckets (strategy :: action :: exit_reason)\n")
    lines.append("| strategy | action | exit_reason | n | wins | pnl | avg_pnl |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for key, row in ranked[:25]:
        parts = key.split("::", 2)
        while len(parts) < 3:
            parts.append("")
        s, a, er = parts[0], parts[1], parts[2]
        lines.append(
            f"| {s} | {a} | {er} | {row['n']} | {row['wins']} | "
            f"{row['pnl']:.4f} | {row['avg_pnl']:.4f} |"
        )

    tags = payload.get("eth_side_src_exit", {})
    if tags:
        lines.append("\n## ETH `eth_macro` — side_src × exit_reason\n")
        lines.append("| side_src | exit_reason | n | pnl | avg_pnl |")
        lines.append("|---|---|---:|---:|---:|")
        for key, row in sorted(tags.items(), key=lambda kv: kv[1]["pnl"]):
            ss, er = key.split("::", 1) if "::" in key else (key, "")
            lines.append(
                f"| {ss or '—'} | {er} | {row['n']} | {row['pnl']:.4f} | "
                f"{row['avg_pnl']:.4f} |"
            )

    diag = payload.get("counter_trend_btc_bitcoin", {})
    if diag:
        lines.append("\n## BTC counter-trend subset (`bitcoin`)\n")
        lines.append(
            f"- n={diag.get('n')}, wins={diag.get('wins')}, "
            f"pnl={diag.get('pnl')}, win_rate={diag.get('win_rate')}\n"
        )

    exs = payload.get("exit_stratification")
    if exs:
        lines.append("\n## Exit stratification (Hermes buckets)\n")
        ov = exs.get("overall", {})
        lines.append(
            f"- **Overall:** `take_profit`={ov.get('take_profit')}, "
            f"`updown_time_stop`={ov.get('updown_time_stop')}, "
            f"`RESOLVED:YES`={ov.get('RESOLVED:YES (real)')}, "
            f"`RESOLVED:NO`={ov.get('RESOLVED:NO (real)')}, "
            f"`updown_expired`={ov.get('updown_expired')}, "
            f"`other`={ov.get('other')}; "
            f"**tp_share** (TP / sum of non-other buckets)={ov.get('tp_share_of_hermes_buckets')}\n"
        )
        lines.append("\n### By strategy\n")
        lines.append(
            "| strategy | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for sk, row in exs.get("by_strategy", {}).items():
            lines.append(
                f"| {sk} | {row.get('take_profit')} | {row.get('updown_time_stop')} | "
                f"{row.get('RESOLVED:YES (real)')} | {row.get('RESOLVED:NO (real)')} | "
                f"{row.get('updown_expired')} | {row.get('other')} | "
                f"{row.get('tp_share_of_hermes_buckets')} |"
            )
        lines.append("\n### By strategy × window_size\n")
        lines.append(
            "| strategy::window | TP | time_stop | RESOLVED:Y | RESOLVED:N | expired | other | tp_share |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for sk, row in exs.get("by_strategy_window", {}).items():
            lines.append(
                f"| {sk} | {row.get('take_profit')} | {row.get('updown_time_stop')} | "
                f"{row.get('RESOLVED:YES (real)')} | {row.get('RESOLVED:NO (real)')} | "
                f"{row.get('updown_expired')} | {row.get('other')} | "
                f"{row.get('tp_share_of_hermes_buckets')} |"
            )

    lines.append("\n## Sessions\n")
    for s in meta.get("sessions_included", []):
        lines.append(f"- `{s}`")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-session paper trade attribution.")
    ap.add_argument(
        "--paper-root",
        type=Path,
        default=None,
        help="Default: repo_root/data/paper_trades",
    )
    ap.add_argument(
        "--label",
        required=True,
        help="Output stem: attribution_since_<label>.{md,json}",
    )
    ap.add_argument(
        "--since-iso",
        default=None,
        help="Include only EXIT rows with timestamp >= this ISO instant (e.g. 2026-05-04T22:07:45+00:00)",
    )
    ap.add_argument(
        "--from-first-line",
        type=Path,
        default=None,
        help="Use first JSONL line timestamp of this entries.jsonl as --since-iso",
    )
    ap.add_argument(
        "--session-prefix",
        default=None,
        help="Only session dirs whose name starts with this prefix",
    )
    ap.add_argument(
        "--after-mtime",
        default=None,
        help="Only session dirs with mtime on or after YYYY-MM-DD (UTC midnight)",
    )
    ap.add_argument(
        "--sessions",
        default=None,
        help="Comma-separated exact session dir names (under paper-root)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Default: repo_root/docs/session_reports",
    )
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    paper_root = args.paper_root or (repo_root / "data" / "paper_trades")
    out_dir = args.out_dir or (repo_root / "docs" / "session_reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    exit_since: datetime | None = None
    if args.from_first_line:
        exit_since = _first_line_timestamp(args.from_first_line)
        if exit_since is None:
            raise SystemExit(f"Could not read first-line timestamp from {args.from_first_line}")
    elif args.since_iso:
        s = args.since_iso.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        exit_since = datetime.fromisoformat(s)

    explicit = None
    if args.sessions:
        explicit = [x.strip() for x in args.sessions.split(",") if x.strip()]

    session_paths = _discover_sessions(
        paper_root,
        args.session_prefix,
        args.after_mtime,
        explicit,
    )

    all_closed: list[dict[str, Any]] = []
    seen_sessions: list[str] = []
    for sp in session_paths:
        ent = sp / "entries.jsonl"
        seen_sessions.append(sp.name)
        all_closed.extend(closed_rows_from_jsonl(ent, exit_since, sp.name))

    by_sae = _agg_key(
        all_closed,
        lambda r: f"{r['strategy']}::{r['action']}::{r['exit_reason']}",
    )

    eth_only = [r for r in all_closed if r["strategy"] == "eth_macro"]
    eth_tag = _agg_key(
        eth_only,
        lambda r: f"{r.get('side_src') or '—'}::{r['exit_reason']}",
    )

    btc_ct = [r for r in all_closed if r["strategy"] == "bitcoin" and r["counter_trend_btc"]]
    btc_ct_stats: dict[str, Any] = {}
    if btc_ct:
        pnl = sum(x["pnl"] for x in btc_ct)
        wins = sum(1 for x in btc_ct if x["pnl"] > 0)
        n = len(btc_ct)
        btc_ct_stats = {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "pnl": round(pnl, 4),
        }

    payload: dict[str, Any] = {
        "meta": {
            "label": args.label,
            "repo_relative": str(paper_root.relative_to(repo_root)),
            "closed_trades": len(all_closed),
            "sessions_included": seen_sessions,
            "filters": {
                "since_iso": args.since_iso,
                "from_first_line": str(args.from_first_line)
                if args.from_first_line
                else None,
                "session_prefix": args.session_prefix,
                "after_mtime": args.after_mtime,
                "explicit_sessions": explicit,
            },
        },
        "by_strategy_action_exit": by_sae,
        "by_strategy": _agg_key(all_closed, lambda r: r["strategy"]),
        "by_exit_reason": _agg_key(all_closed, lambda r: r["exit_reason"]),
        "eth_side_src_exit": eth_tag,
        "counter_trend_btc_bitcoin": btc_ct_stats or None,
        "exit_stratification": _exit_stratify(all_closed),
        "closed_sample": all_closed[:500],
    }

    json_path = out_dir / f"attribution_since_{args.label}.json"
    md_path = out_dir / f"attribution_since_{args.label}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
