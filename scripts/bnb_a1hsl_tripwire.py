#!/usr/bin/env python3
"""bnb_a1hsl_tripwire.py — watch the 2026-08-14 bnb alt_1h_simple_long quality-veto
and CUT the lane automatically if it does not recover.

WHY THIS EXISTS
---------------
bnb_macro|1h|BUY_YES was the worst lane in the book: n=82, WR 29.3% against a 50.5%
breakeven (z=-3.75, p<0.0002 -> a defect, not variance), -$196.42, per-$1 -0.1211.
Its est_prob_up ran a median 0.573 while the realized up-rate was 29.3% — ANTI-correlated.
It lost at ~19-21% WR whether alt_htf_bias agreed or disagreed, because the cohort is
admitted on PRICE BAND ALONE and is exempt from the quant-agreement gate
(sol_macro.py:3760). On 2026-08-14 we set `quality_veto_enabled: true` for bnb ONLY.

doge_macro carries the IDENTICAL bypass and was deliberately LEFT ON as the control.
If bnb recovers and doge does not, the veto did it. If BOTH move, it was the tape.

THE TRIPWIRE
------------
Baseline is stamped on first run into data/runtime/bnb_a1hsl_tripwire.json, so every
later run measures only trades closed AFTER the veto went live.

CUT if either fires:
  * cumulative post-veto pnl <= -$40  (with n >= 4)      -- fast bleed
  * n >= 12 AND WR < 40%                                 -- still broken at volume
Breakeven is ~50%; 40% leaves room for variance while still catching a broken lane.

CUT ACTION: sets strategies.bnb_macro.disable_buy_yes_1h: true in config/settings.yaml.
That key is read per-scan via self.config.get (sol_macro.py:4017), so it HOT-RELOADS —
no restart, no session wipe. A timestamped backup is written before any edit, and the
YAML is re-parsed afterwards to prove it still loads and that ONLY bnb changed.

This is deliberately NOT a "lane is quiet" alarm. If the veto absorbs the whole bad
cohort the lane may go to zero trades, which is the intended outcome, not a failure —
it is reported, never auto-cut on.

Usage:
  python scripts/bnb_a1hsl_tripwire.py            # evaluate; CUT if tripped (armed)
  python scripts/bnb_a1hsl_tripwire.py --dry-run  # evaluate + report, never write
  python scripts/bnb_a1hsl_tripwire.py --status   # just print current standing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TRADES = REPO / "data" / "calibration" / "trades.jsonl"
REJECTS = REPO / "data" / "calibration" / "rejected_candidates.jsonl"
SETTINGS = REPO / "config" / "settings.yaml"
STATE = REPO / "data" / "runtime" / "bnb_a1hsl_tripwire.json"

STRATEGY, WINDOW, ACTION = "bnb_macro", "1h", "BUY_YES"
PNL_FLOOR = -40.0     # cumulative post-veto pnl that trips the cut
PNL_FLOOR_MIN_N = 4
WR_MIN_N = 12
WR_FLOOR = 0.40

# Pre-veto baseline, for the report only.
PRE_N, PRE_WR, PRE_PNL = 82, 0.293, -196.42


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            pass
    return {}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(STATE)


def _iter_trades():
    if not TRADES.exists():
        return
    with open(TRADES, encoding="utf-8") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _post_veto_trades(baseline: str) -> list[dict]:
    out = []
    for r in _iter_trades():
        if (
            r.get("strategy") == STRATEGY
            and r.get("window") == WINDOW
            and r.get("action") == ACTION
            and r.get("pnl") is not None
            and (r.get("closed_at") or "") > baseline
        ):
            out.append(r)
    return out


def _veto_hits(baseline: str) -> int:
    """How many candidates the veto actually rejected since baseline."""
    if not REJECTS.exists():
        return 0
    n = 0
    with open(REJECTS, encoding="utf-8") as fh:
        for line in fh:
            if "alt_1h_simple_long_quality_veto" not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("strategy") == STRATEGY and (r.get("ts") or "") > baseline:
                n += 1
    return n


def _page(msg: str) -> None:
    """Best-effort page. Never fatal — a dead pager must not block the cut."""
    for cmd in (
        ["hermes", "send", "--to", "telegram", "--message", msg],
        [str(Path.home() / ".hermes/node/bin/hermes"), "send", "--to", "telegram", "--message", msg],
    ):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=25).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            continue
    print("[tripwire] WARNING: could not page", file=sys.stderr)


def _apply_cut(dry_run: bool) -> bool:
    """Set strategies.bnb_macro.disable_buy_yes_1h: true. Returns True if written."""
    import yaml  # local import so --status works without a full env

    text = SETTINGS.read_text()
    lines = text.split("\n")

    # Locate the bnb_macro block by walking top-level strategy keys, so we can never
    # land in doge's (byte-identical alt_1h_simple_long block — this bit us once).
    start = end = None
    for i, ln in enumerate(lines):
        if ln.startswith("  bnb_macro:"):
            start = i
        elif start is not None and ln.startswith("  ") and not ln.startswith("   ") and ln.strip().endswith(":"):
            if i > start:
                end = i
                break
    if start is None:
        print("[tripwire] ERROR: bnb_macro block not found", file=sys.stderr)
        return False
    end = end if end is not None else len(lines)

    for i in range(start, end):
        if lines[i].strip().startswith("disable_buy_yes_1h:"):
            if "true" in lines[i].lower():
                print("[tripwire] already cut — nothing to do")
                return False
            lines[i] = "    disable_buy_yes_1h: true"
            break
    else:
        lines.insert(start + 1, "    disable_buy_yes_1h: true")

    lines.insert(
        start + 1,
        f"    # 2026-08-14 TRIPWIRE AUTO-CUT ({_utcnow()}): the alt_1h_simple_long "
        f"quality-veto did NOT rescue bnb 1h BUY_YES. See scripts/bnb_a1hsl_tripwire.py.",
    )
    new = "\n".join(lines)

    if dry_run:
        print("[tripwire] DRY-RUN: would set bnb_macro.disable_buy_yes_1h: true")
        return False

    backup = SETTINGS.with_name(
        f"settings.yaml.bak_tripwire_bnbcut_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(SETTINGS, backup)
    SETTINGS.write_text(new)

    # Prove it still parses and that ONLY bnb changed.
    try:
        cfg = yaml.safe_load(SETTINGS.read_text())["strategies"]
    except Exception as exc:  # noqa: BLE001 — any parse failure must roll back
        shutil.copy2(backup, SETTINGS)
        print(f"[tripwire] ERROR: YAML broke ({exc}); ROLLED BACK from {backup.name}", file=sys.stderr)
        return False
    if not cfg["bnb_macro"].get("disable_buy_yes_1h"):
        shutil.copy2(backup, SETTINGS)
        print("[tripwire] ERROR: cut did not take; ROLLED BACK", file=sys.stderr)
        return False
    for peer in ("doge_macro", "sol_macro", "xrp_macro", "hype_macro", "eth_macro"):
        if cfg.get(peer, {}).get("disable_buy_yes_1h"):
            shutil.copy2(backup, SETTINGS)
            print(f"[tripwire] ERROR: {peer} also changed; ROLLED BACK", file=sys.stderr)
            return False
    print(f"[tripwire] CUT APPLIED (backup {backup.name}) — hot-reloads, no restart")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="evaluate but never write config")
    ap.add_argument("--status", action="store_true", help="print standing only, never cut")
    args = ap.parse_args()

    st = _load_state()
    if not st.get("baseline"):
        st = {"baseline": _utcnow(), "armed_at": _utcnow(), "cut": False}
        _save_state(st)
        print(f"[tripwire] ARMED. baseline={st['baseline']}")
        print("  measuring bnb_macro|1h|BUY_YES closed AFTER this instant.")
        return 0

    baseline = st["baseline"]
    rows = _post_veto_trades(baseline)
    n = len(rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    pnl = sum(r["pnl"] for r in rows)
    wr = (wins / n) if n else 0.0
    vetoed = _veto_hits(baseline)

    print(f"=== bnb alt_1h_simple_long tripwire — {_utcnow()} ===")
    print(f"  baseline (veto live): {baseline}")
    print(f"  PRE-veto  : n={PRE_N} WR={PRE_WR:.1%} pnl=${PRE_PNL:+.2f}")
    print(f"  POST-veto : n={n} WR={wr:.1%} pnl=${pnl:+.2f}  wins={wins}")
    print(f"  candidates vetoed since baseline: {vetoed}")
    print(f"  already cut: {st.get('cut', False)}")

    if st.get("cut"):
        print("  -> lane already cut; nothing to do")
        return 0

    if n == 0:
        print("  -> no post-veto closes yet."
              + (f" Veto is firing ({vetoed} absorbed) — working as intended."
                 if vetoed else " Veto has not fired either; lane simply quiet."))
        return 0

    trip = None
    if n >= PNL_FLOOR_MIN_N and pnl <= PNL_FLOOR:
        trip = f"cumulative pnl ${pnl:+.2f} <= ${PNL_FLOOR:+.2f} at n={n}"
    elif n >= WR_MIN_N and wr < WR_FLOOR:
        trip = f"WR {wr:.1%} < {WR_FLOOR:.0%} at n={n}"

    if not trip:
        need = []
        if n < WR_MIN_N:
            need.append(f"{WR_MIN_N - n} more closes for the WR test")
        print(f"  -> HOLDING. not tripped." + (f" ({'; '.join(need)})" if need else ""))
        return 0

    print(f"  -> TRIPPED: {trip}")
    if args.status:
        print("     (--status: not cutting)")
        return 0

    applied = _apply_cut(args.dry_run)
    if applied:
        st["cut"] = True
        st["cut_at"] = _utcnow()
        st["cut_reason"] = trip
        st["cut_stats"] = {"n": n, "wr": wr, "pnl": pnl}
        _save_state(st)
        _page(
            f"PSB TRIPWIRE: bnb 1h BUY_YES CUT.\n{trip}\n"
            f"post-veto n={n} WR={wr:.1%} pnl=${pnl:+.2f} (pre: n={PRE_N} WR={PRE_WR:.1%} ${PRE_PNL:+.2f}).\n"
            f"alt_1h_simple_long quality-veto did NOT rescue it. "
            f"disable_buy_yes_1h=true, hot-reloaded, no restart."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
