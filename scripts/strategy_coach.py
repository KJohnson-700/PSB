"""
Strategy Coach — self-improving feedback loop for the Polymarket bot.

Reads closed trade data from all journal sessions, finds statistical patterns,
then uses Claude (Opus) to interpret them and propose specific config changes.

Usage:
  python scripts/strategy_coach.py                  # full run with AI
  python scripts/strategy_coach.py --no-ai          # pattern report only
  python scripts/strategy_coach.py --days-back 14   # last 14 days
"""

import argparse
import asyncio
import json
import os
import sys
import yaml
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

# Repo root on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.env_bootstrap import load_project_dotenv
from src.journal_features import hold_bucket

load_project_dotenv(ROOT, quiet=True)

DATA_DIR = ROOT / "data"
COACH_DIR = DATA_DIR / "coach"
REPORTS_DIR = COACH_DIR / "pattern_reports"
CHANGE_LOG = COACH_DIR / "change_log.jsonl"
CONFIG_PATH = ROOT / "config" / "settings.yaml"
SESSIONS_DIR = DATA_DIR / "paper_trades"

MIN_SEGMENT_TRADES = 5   # don't report on segments with fewer trades
WEAK_WR_THRESHOLD = 0.46  # flag segments below this WR
WEAK_EV_THRESHOLD = -3.0  # flag segments with EV below this $ per trade


# ---------------------------------------------------------------------------
# Trade loading
# ---------------------------------------------------------------------------

def load_all_trades(days_back: int = 30) -> list[dict]:
    """
    Walk all paper_trade session dirs, read entries.jsonl, return EXIT events
    as closed-trade dicts. Preserves the `extra` dict from signal features.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    trades = []

    if not SESSIONS_DIR.exists():
        return trades

    for session_dir in sorted(SESSIONS_DIR.iterdir()):
        entries_file = session_dir / "entries.jsonl"
        if not entries_file.exists():
            continue

        open_entries: dict[str, dict] = {}

        with open(entries_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event = e.get("event", "")
                tid = e.get("trade_id", "")

                if event == "ENTRY":
                    open_entries[tid] = e

                elif event == "EXIT":
                    pnl = e.get("pnl", 0) or 0
                    ep = e.get("entry_price", 0) or 0
                    cp = e.get("current_price", 0) or 0
                    # Skip phantom exits (token-flip bug)
                    if ep > 0 and abs(ep + cp - 1.0) < 0.02:
                        continue
                    if abs(pnl) > 200:
                        continue

                    # Parse closed_at
                    ts_str = e.get("timestamp", "")
                    try:
                        closed_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        closed_at = None

                    if closed_at and closed_at.tzinfo is None:
                        closed_at = closed_at.replace(tzinfo=timezone.utc)

                    if closed_at and closed_at < cutoff:
                        continue

                    # Merge extra from ENTRY event if not on EXIT
                    extra = e.get("extra") or {}
                    if not extra and tid in open_entries:
                        extra = open_entries[tid].get("extra") or {}

                    hour_utc = extra.get("hour_utc_entry", extra.get("hour_utc"))
                    hour_pt = extra.get("hour_pt")
                    hour_utc_exit = extra.get("hour_utc_exit")
                    hour_pt_exit = extra.get("hour_pt_exit")
                    if closed_at:
                        if hour_utc_exit is None:
                            hour_utc_exit = closed_at.hour
                        if hour_pt_exit is None and ZoneInfo is not None:
                            hour_pt_exit = closed_at.astimezone(
                                ZoneInfo("America/Los_Angeles")
                            ).hour
                        if hour_utc is None:
                            hour_utc = hour_utc_exit
                        if hour_pt is None:
                            hour_pt = hour_pt_exit

                    trade = {
                        "trade_id": tid,
                        "market_id": e.get("market_id"),
                        "market_question": e.get("market_question", ""),
                        "strategy": e.get("strategy", ""),
                        "action": e.get("action", ""),
                        "size": e.get("size", 0),
                        "entry_price": ep,
                        "exit_price": cp,
                        "pnl": pnl,
                        "closed_at": ts_str,
                        "exit_reason": e.get("reason", ""),
                        # Signal features (may be empty for old trades)
                        "hour_utc": hour_utc,
                        "hour_pt": hour_pt,
                        "hour_utc_exit": hour_utc_exit,
                        "hour_pt_exit": hour_pt_exit,
                        "hold_seconds": extra.get("hold_seconds"),
                        "hold_bucket": hold_bucket(extra.get("hold_seconds")),
                        "minutes_to_market_end": extra.get("minutes_to_market_end"),
                        "window_size": extra.get("window_size"),
                        "htf_bias": extra.get("htf_bias"),
                        "ai_used": extra.get("ai_used"),
                        "ai_confidence": extra.get("ai_confidence"),
                        "yes_price": extra.get("yes_price"),
                        "btc_price": extra.get("btc_price"),
                        "lag_magnitude": extra.get("lag_magnitude"),
                        "edge": extra.get("edge"),
                    }
                    trades.append(trade)

    return trades


# ---------------------------------------------------------------------------
# Segmentation helpers
# ---------------------------------------------------------------------------

def _seg_stats(trades: list[dict]) -> dict:
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trades)
    n = len(trades)
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "wr": wins / n if n else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / n, 2) if n else 0,
    }


def segment_trades(trades: list[dict]) -> dict:
    """Return nested stats dicts segmented by every useful dimension."""
    segs: dict[str, dict] = {}

    # --- By strategy ---
    by_strat: dict[str, list] = defaultdict(list)
    for t in trades:
        by_strat[t["strategy"]].append(t)
    segs["by_strategy"] = {k: _seg_stats(v) for k, v in by_strat.items()}

    # --- By strategy + action ---
    by_strat_action: dict[str, list] = defaultdict(list)
    for t in trades:
        key = f"{t['strategy']}|{t['action']}"
        by_strat_action[key].append(t)
    segs["by_strategy_action"] = {k: _seg_stats(v) for k, v in by_strat_action.items()}

    # --- By strategy + window ---
    by_strat_win: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["window_size"]:
            key = f"{t['strategy']}|{t['window_size']}"
            by_strat_win[key].append(t)
    segs["by_strategy_window"] = {k: _seg_stats(v) for k, v in by_strat_win.items()}

    # --- By strategy + hour ---
    by_strat_hour: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["hour_utc"] is not None:
            key = f"{t['strategy']}|H{t['hour_utc']:02d}"
            by_strat_hour[key].append(t)
    segs["by_strategy_hour"] = {k: _seg_stats(v) for k, v in by_strat_hour.items()}

    # --- By strategy + Pacific hour (entry) ---
    by_strat_hour_pt: dict[str, list] = defaultdict(list)
    for t in trades:
        hp = t.get("hour_pt")
        if hp is not None:
            key = f"{t['strategy']}|PT{int(hp):02d}"
            by_strat_hour_pt[key].append(t)
    segs["by_strategy_hour_pt"] = {k: _seg_stats(v) for k, v in by_strat_hour_pt.items()}

    # --- By strategy + UTC hour of exit ---
    by_strat_hour_exit: dict[str, list] = defaultdict(list)
    for t in trades:
        hx = t.get("hour_utc_exit")
        if hx is not None:
            key = f"{t['strategy']}|XH{int(hx):02d}"
            by_strat_hour_exit[key].append(t)
    segs["by_strategy_hour_exit_utc"] = {k: _seg_stats(v) for k, v in by_strat_hour_exit.items()}

    # --- By hold-time bucket ---
    by_hold: dict[str, list] = defaultdict(list)
    for t in trades:
        b = t.get("hold_bucket")
        if b:
            key = f"{t['strategy']}|{b}"
            by_hold[key].append(t)
    segs["by_strategy_hold"] = {k: _seg_stats(v) for k, v in by_hold.items()}

    # --- By minutes to market end (entry) — quantiles as coarse bins ---
    by_tte: dict[str, list] = defaultdict(list)
    for t in trades:
        m = t.get("minutes_to_market_end")
        if m is None:
            continue
        if m < 15:
            bin_ = "lte15m"
        elif m < 60:
            bin_ = "15-60m"
        elif m < 240:
            bin_ = "1-4h"
        else:
            bin_ = "4h+"
        key = f"{t['strategy']}|{bin_}"
        by_tte[key].append(t)
    segs["by_strategy_tte_bin"] = {k: _seg_stats(v) for k, v in by_tte.items()}

    # --- By strategy + htf_bias ---
    by_strat_htf: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["htf_bias"]:
            key = f"{t['strategy']}|{t['htf_bias']}"
            by_strat_htf[key].append(t)
    segs["by_strategy_htf"] = {k: _seg_stats(v) for k, v in by_strat_htf.items()}

    # --- By strategy + action + htf_bias (triple interaction) ---
    by_triple: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["htf_bias"]:
            key = f"{t['strategy']}|{t['action']}|{t['htf_bias']}"
            by_triple[key].append(t)
    segs["by_strategy_action_htf"] = {k: _seg_stats(v) for k, v in by_triple.items()}

    # --- By exit reason ---
    by_exit: dict[str, list] = defaultdict(list)
    for t in trades:
        key = t["exit_reason"] or "?"
        # Normalize RESOLVED:YES / RESOLVED:NO
        if "RESOLVED:YES" in key:
            key = "RESOLVED:YES"
        elif "RESOLVED:NO" in key:
            key = "RESOLVED:NO"
        elif "stop_loss" in key:
            key = "stop_loss"
        elif "take_profit" in key:
            key = "take_profit"
        by_exit[key].append(t)
    segs["by_exit_reason"] = {k: _seg_stats(v) for k, v in by_exit.items()}

    return segs


def find_weak_segments(segs: dict, min_trades: int = MIN_SEGMENT_TRADES) -> list[dict]:
    """Return segments that are statistically weak (low WR or negative EV)."""
    findings = []
    for dimension, dim_segs in segs.items():
        for key, stats in dim_segs.items():
            if stats["trades"] < min_trades:
                continue
            is_weak = (
                stats["wr"] < WEAK_WR_THRESHOLD
                or stats["avg_pnl"] < WEAK_EV_THRESHOLD
            )
            if is_weak:
                findings.append({
                    "dimension": dimension,
                    "segment": key,
                    "trades": stats["trades"],
                    "wr": round(stats["wr"], 3),
                    "total_pnl": stats["total_pnl"],
                    "avg_pnl": stats["avg_pnl"],
                    "severity": "HIGH" if stats["wr"] < 0.40 or stats["avg_pnl"] < -5 else "MEDIUM",
                })

    # Sort: high severity first, then by total_pnl ascending (worst first)
    findings.sort(key=lambda x: (x["severity"] != "HIGH", x["total_pnl"]))
    return findings


def load_backtest_comparison() -> dict:
    """Load most recent backtest reports and return per-strategy WR."""
    bt_dir = DATA_DIR / "backtest" / "reports"
    if not bt_dir.exists():
        return {}

    newest: dict[str, dict] = {}
    for f in bt_dir.glob("backtest_crypto_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            key = f"{d['symbol']}_{d['window_minutes']}m"
            ts = f.stem.split("_")[-1]
            if key not in newest or ts > newest[key]["_ts"]:
                d["_ts"] = ts
                newest[key] = d
        except Exception:
            continue

    result = {}
    for key, d in newest.items():
        result[key] = {
            "backtest_wr": round(d.get("win_rate", 0), 3),
            "backtest_pnl": round(d.get("net_pnl", 0), 2),
            "backtest_trades": d.get("trades_count", 0),
        }
    return result


def build_report(
    trades: list[dict],
    segs: dict,
    weak: list[dict],
    bt_compare: dict,
    days_back: int,
) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    has_features = sum(
        1
        for t in trades
        if t.get("hour_utc") is not None
        or t.get("hour_pt") is not None
        or t.get("hold_seconds") is not None
    )

    # Overall stats
    overall = _seg_stats(trades)

    # Per-strategy live WR for bt divergence
    bt_divergences = []
    live_by_strat = segs.get("by_strategy", {})
    strategy_window_map = {
        "bitcoin": [("BTC", 15), ("BTC", 5)],
        "sol_lag": [("SOL", 15), ("SOL", 5)],
        "eth_lag": [("ETH", 15), ("ETH", 5)],
    }
    for strat, windows in strategy_window_map.items():
        live_stats = live_by_strat.get(strat, {})
        live_wr = live_stats.get("wr")
        for sym, wm in windows:
            bt_key = f"{sym}_{wm}m"
            if bt_key in bt_compare and live_wr is not None:
                bt_wr = bt_compare[bt_key]["backtest_wr"]
                gap = live_wr - bt_wr
                if abs(gap) > 0.08:  # only flag significant divergences
                    bt_divergences.append({
                        "strategy": strat,
                        "window": f"{wm}m",
                        "live_wr": round(live_wr, 3),
                        "backtest_wr": bt_wr,
                        "gap": round(gap, 3),
                        "direction": "live_worse" if gap < 0 else "live_better",
                    })

    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_back": days_back,
        "total_trades": len(trades),
        "trades_with_features": has_features,
        "overall": overall,
        "weak_segments": weak,
        "bt_divergences": bt_divergences,
        "segments": segs,
        "backtest_data": bt_compare,
    }


def save_report(report: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{report['run_id']}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AI insight layer
# ---------------------------------------------------------------------------

async def get_ai_insights(report: dict, config: dict) -> Optional[dict]:
    """Call Claude Opus with the pattern report and get config change proposals."""
    try:
        import anthropic
    except ImportError:
        print("  [coach] anthropic package not installed — skipping AI. Run: pip install anthropic")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY") or _read_secret("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [coach] No ANTHROPIC_API_KEY in env / .env / secrets.env — skipping AI insights.")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    # Trim config to just the strategy section
    strat_config = config.get("strategies", {})
    relevant_config = {
        k: v for k, v in strat_config.items()
        if k in ("bitcoin", "sol_lag", "eth_lag", "xrp_dump_hedge")
    }

    # Build concise report subset for the prompt
    prompt_data = {
        "total_trades": report["total_trades"],
        "days_back": report["days_back"],
        "overall": report["overall"],
        "weak_segments": report["weak_segments"][:20],  # top 20 worst
        "bt_divergences": report["bt_divergences"],
        "by_strategy": report["segments"].get("by_strategy", {}),
        "by_strategy_action": report["segments"].get("by_strategy_action", {}),
        "by_exit_reason": report["segments"].get("by_exit_reason", {}),
    }

    change_history = load_change_history(n=10)

    system = """You are an expert quantitative trading analyst specializing in
prediction market microstructure. You analyze statistical patterns from a live
paper-trading bot on Polymarket and propose specific, measurable config changes.

The bot trades crypto Up/Down prediction markets using lag correlation signals
(BTC leads SOL/ETH price moves). Strategies: bitcoin (BTC 15m/5m updown),
sol_lag (SOL 15m/5m), eth_lag (ETH 15m/5m).

Key config knobs available:
- blocked_utc_hours_updown: list of UTC hours to skip (per strategy)
- min_edge: minimum quant edge required to enter (per strategy)
- min_edge_5m: same for 5m windows
- disable_sell_yes / disable_buy_yes: block one direction entirely
- min_lag_magnitude_pct: minimum BTC-SOL/ETH lag % required
- entry_price_min / entry_price_max: price band filter
- stop_loss_pct / take_profit_pct: in trading.exit_rules

Output ONLY valid JSON, no prose outside the JSON block."""

    user = f"""=== CURRENT STRATEGY CONFIG ===
{yaml.dump(relevant_config, default_flow_style=False)}

=== PERFORMANCE REPORT ({report['days_back']} days, {report['total_trades']} trades) ===
{json.dumps(prompt_data, indent=2)}

=== RECENT CONFIG CHANGES (last 10) ===
{json.dumps(change_history, indent=2)}

=== YOUR TASK ===
Analyze the weak segments and backtest divergences. Propose up to 6 specific
config changes that would improve win rate and/or expected value.

Consider:
1. Are there clear hour/action/HTF combinations that should be blocked?
2. Should stop losses be disabled for short-duration (5-15m) markets?
3. Is the lag magnitude threshold too loose?
4. Is the entry price band wrong?
5. Should any direction (SELL_YES/BUY_YES) be gated on HTF bias?

For each proposal:
- config_path: exact dot-notation path (e.g. "strategies.bitcoin.blocked_utc_hours_updown")
- current_value: what it is now
- proposed_value: your recommendation
- rationale: 1-2 sentences citing specific data from the report
- expected_impact: e.g. "removes 12 losing trades/month, saves ~$35"
- confidence: "high" | "medium" | "low"
- min_trades_to_validate: sample size needed to confirm the change worked

Respond ONLY with JSON:
{{
  "summary": "2-3 sentence executive summary of what the data shows",
  "biggest_problems": ["problem 1", "problem 2", "problem 3"],
  "recommendations": [
    {{
      "config_path": "...",
      "current_value": ...,
      "proposed_value": ...,
      "rationale": "...",
      "expected_impact": "...",
      "confidence": "...",
      "min_trades_to_validate": 30
    }}
  ]
}}"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [coach] AI call failed: {e}")
        return None


def _read_secret(key: str) -> Optional[str]:
    """Fallback: read a key from `.env` or config/secrets.env (no shell export)."""
    for path in (ROOT / ".env", ROOT / "config" / "secrets.env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

def load_change_history(n: int = 10) -> list[dict]:
    if not CHANGE_LOG.exists():
        return []
    lines = CHANGE_LOG.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
        if len(entries) >= n:
            break
    return list(reversed(entries))


def log_proposal(run_id: str, recommendations: list[dict]) -> None:
    COACH_DIR.mkdir(parents=True, exist_ok=True)
    for rec in recommendations:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "config_path": rec.get("config_path"),
            "current_value": rec.get("current_value"),
            "proposed_value": rec.get("proposed_value"),
            "rationale": rec.get("rationale"),
            "expected_impact": rec.get("expected_impact"),
            "confidence": rec.get("confidence"),
            "applied": False,
            "post_change_wr": None,
        }
        with open(CHANGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------

def print_report(report: dict, ai_insights: Optional[dict] = None) -> None:
    print("\n" + "=" * 70)
    print("  STRATEGY COACH REPORT")
    print(f"  {report['total_trades']} trades | last {report['days_back']} days | "
          f"{report['trades_with_features']} have timing features (hour / hold)")
    print("=" * 70)

    overall = report["overall"]
    print(f"\nOVERALL: {overall['trades']} trades  WR={overall['wr']*100:.1f}%  "
          f"PnL=${overall['total_pnl']:+.2f}  avg=${overall['avg_pnl']:+.2f}/trade\n")

    print("BY STRATEGY:")
    for k, v in report["segments"].get("by_strategy", {}).items():
        print(f"  {k:<12}  trades={v['trades']:>3}  WR={v['wr']*100:>5.1f}%  "
              f"PnL=${v['total_pnl']:>+8.2f}")

    print("\nBY ACTION:")
    for k, v in sorted(report["segments"].get("by_strategy_action", {}).items()):
        if v["trades"] < 3:
            continue
        print(f"  {k:<25}  trades={v['trades']:>3}  WR={v['wr']*100:>5.1f}%  "
              f"PnL=${v['total_pnl']:>+8.2f}")

    # ── Hourly breakdown (worst 5 hours per strategy) ──
    hour_segs = report["segments"].get("by_strategy_hour", {})
    if hour_segs:
        print("\nWORST HOURS BY STRATEGY (>= 5 trades, WR < 50%):")
        by_strat: dict = {}
        for key, v in hour_segs.items():
            if v["trades"] < 5:
                continue
            strat, hour_label = key.split("|", 1)
            by_strat.setdefault(strat, []).append((hour_label, v))
        for strat in sorted(by_strat):
            rows = sorted(by_strat[strat], key=lambda x: x[1]["wr"])
            bad  = [(h, v) for h, v in rows if v["wr"] < 0.50][:5]
            if bad:
                print(f"  {strat}:")
                for h, v in bad:
                    print(f"    {h}  trades={v['trades']:>3}  WR={v['wr']*100:>5.1f}%  "
                          f"PnL=${v['total_pnl']:>+7.2f}  avg=${v['avg_pnl']:>+.2f}/t")

    print("\nWEAK SEGMENTS (flagged for review):")
    for w in report["weak_segments"][:15]:
        sev = "[!!]" if w["severity"] == "HIGH" else "[ !]"
        print(f"  {sev} {w['segment']:<35}  trades={w['trades']:>3}  "
              f"WR={w['wr']*100:>5.1f}%  PnL=${w['total_pnl']:>+8.2f}")

    if report["bt_divergences"]:
        print("\nBACKTEST vs LIVE DIVERGENCES:")
        for d in report["bt_divergences"]:
            arrow = "v" if d["direction"] == "live_worse" else "^"
            print(f"  {d['strategy']} {d['window']}  BT={d['backtest_wr']*100:.1f}%  "
                  f"Live={d['live_wr']*100:.1f}%  gap={d['gap']*100:+.1f}% {arrow}")

    if ai_insights:
        print("\n" + "=" * 70)
        print("  AI COACH INSIGHTS (Claude Opus)")
        print("=" * 70)
        print(f"\nSUMMARY: {ai_insights.get('summary', '')}\n")
        print("BIGGEST PROBLEMS:")
        for p in ai_insights.get("biggest_problems", []):
            print(f"  - {p}")
        print("\nRECOMMENDATIONS:")
        for i, rec in enumerate(ai_insights.get("recommendations", []), 1):
            conf = rec.get("confidence", "?").upper()
            print(f"\n  [{i}] {conf} — {rec.get('config_path')}")
            print(f"       Current:  {rec.get('current_value')}")
            print(f"       Proposed: {rec.get('proposed_value')}")
            print(f"       Why:      {rec.get('rationale')}")
            print(f"       Impact:   {rec.get('expected_impact')}")

    print("\n" + "=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(use_ai: bool = True, days_back: int = 30, min_trades: int = 10) -> None:
    print(f"[coach] Loading trades (last {days_back} days)...")
    trades = load_all_trades(days_back=days_back)

    if len(trades) < min_trades:
        print(f"[coach] Only {len(trades)} trades found (need {min_trades}). Run with more data.")
        return

    print(f"[coach] {len(trades)} trades loaded. Segmenting...")
    segs = segment_trades(trades)
    weak = find_weak_segments(segs)
    bt_compare = load_backtest_comparison()

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    report = build_report(trades, segs, weak, bt_compare, days_back)
    report_path = save_report(report)
    print(f"[coach] Report saved: {report_path}")

    ai_insights = None
    if use_ai:
        print("[coach] Calling Claude Opus for insights...")
        ai_insights = asyncio.run(get_ai_insights(report, config))
        if ai_insights and ai_insights.get("recommendations"):
            log_proposal(report["run_id"], ai_insights["recommendations"])
            print(f"[coach] {len(ai_insights['recommendations'])} proposals logged to change_log.jsonl")

    print_report(report, ai_insights)

    # Write latest report path to memory for Claude to pick up
    mem_path = ROOT / "data" / "coach" / "latest_report.json"
    mem_path.write_text(json.dumps({
        "path": str(report_path),
        "run_id": report["run_id"],
        "generated_at": report["generated_at"],
        "total_trades": report["total_trades"],
        "overall_wr": report["overall"]["wr"],
        "overall_pnl": report["overall"]["total_pnl"],
        "weak_segment_count": len(report["weak_segments"]),
        "ai_summary": ai_insights.get("summary") if ai_insights else None,
        "recommendations": ai_insights.get("recommendations") if ai_insights else [],
    }, indent=2), encoding="utf-8")
    print(f"[coach] Latest report pointer: data/coach/latest_report.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strategy Coach — pattern analysis + AI insights")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI call, pattern report only")
    parser.add_argument("--days-back", type=int, default=30, help="Days of history to analyze")
    parser.add_argument("--min-trades", type=int, default=10, help="Minimum trades required")
    args = parser.parse_args()
    main(use_ai=not args.no_ai, days_back=args.days_back, min_trades=args.min_trades)
