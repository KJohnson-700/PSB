#!/usr/bin/env python3
"""decision_writepoint_inventory.py — map every place a trade decision is made against
every place one is RECORDED. Design input for unifying the Decision Packet.

THE PROBLEM THIS MEASURES
─────────────────────────
Two mechanisms decide whether a candidate becomes a trade:

  _bump_skip(reason)         a LOCAL closure that increments an in-memory counter.
                             Leaves NO record: no market, no price, no est_prob, no side,
                             no policy version. The count is summarised into the ops_pulse
                             log line and then lost.
  log_rejected_candidate()   the real decision record — market, yes/no price, est_prob,
                             side + side_source + resolver_path, effective_min_edge,
                             gate_stage, policy_version, feature_hash.

AST count is 34 log call sites against 188 skip sites (a grep said 43/191 — it also counted
the closure definition and mentions inside comments; trust the AST). But SITES are the wrong
unit: a silent branch that never fires costs nothing, while one firing hundreds of times a
day is the whole blind spot. So this ranks by FIRING VOLUME from ops_pulse
`top_skip_reasons`, cross-checked against reasons that actually reach the reject log.

RECEIPTS — verified against a 27,175-row rejected_candidates.jsonl, all at ZERO rows while
firing live:
    kelly_nonpositive         487    price_too_far_from_50_50   241
    rsi_overbought_5m         314    ltf_confirmed_late_entry   122
Same class as the documented "reject log is BLIND for BTC" (favorite-policy skips) and the
lane_management pauses, which are enforced at exposure_manager AFTER candidate generation
and emit nothing at all.

⛔ `lane_min_edge` is NOT one of them, though an earlier pass of mine said so. That reading
was taken minutes after a log rotation, off a 1,823-row file; on the full log it has 760
rows and is properly recorded. Do not re-derive coverage from a freshly rotated log.

⚠️ VOLUME IS A LOWER BOUND, NOT A CENSUS. `top_skip_reasons` is truncated per cycle —
measured 1-6 reasons per ops_pulse line, usually 2-3. So a reason that is rarely in a
cycle's top few is under-counted, and the headline "% unrecorded" is directional only. It
is good enough to RANK what to instrument first; it is not a coverage percentage to quote.

⚠️ COVERAGE IS A PROXIMITY HEURISTIC. Both call kinds live inside one enormous scan-loop
function, so "same function" tells you nothing. A skip is counted as COVERED when a
log_rejected_candidate call sits within PAIR_WINDOW lines of it. That will misjudge some
sites in both directions — treat the per-site verdict as a lead, and the reject-log
cross-check as the ground truth.

READ-ONLY. Static analysis plus log reads. Writes nothing, imports nothing from the bot.

USAGE
  scripts/decision_writepoint_inventory.py
  scripts/decision_writepoint_inventory.py --json out.json
"""
import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STRAT_DIR = os.path.join(REPO, "src", "strategies")
REJECT = os.path.join(REPO, "data", "calibration", "rejected_candidates.jsonl")
LOGDIR = os.path.join(REPO, "data", "logs")

PAIR_WINDOW = 12          # lines: a log call this close to a skip counts as pairing it
SKIP_FN = "_bump_skip"
# Both writers count. `log_skip_packet` (2026-08-17) is the never-raising wrapper used to
# close the blind-spot sites; it delegates to log_rejected_candidate, so counting only the
# latter made the newly-instrumented gates read as still-silent.
LOG_FNS = ("log_rejected_candidate", "log_skip_packet")


def reason_of(node):
    """Best-effort reason string for a _bump_skip(...) call.

    Three real shapes in this codebase:
      _bump_skip("neutral_bias")                      -> literal
      _bump_skip(f"buy_no_{_updown_tf}_disabled_lane") -> template, normalise the hole
      _bump_skip(_sp.get("skip") or "favorite_policy_skip") -> dynamic, keep the fallback
    """
    if not node.args:
        return None, "none"
    a = node.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value, "literal"
    if isinstance(a, ast.JoinedStr):
        parts = []
        for v in a.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return "".join(parts), "template"
    if isinstance(a, ast.BoolOp):          # `x.get(...) or "fallback"`
        for v in reversed(a.values):
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return v.value, "dynamic"
    return None, "dynamic"


def scan_file(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            src = fh.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return [], []
    skips, logs = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == SKIP_FN:
            reason, kind = reason_of(node)
            skips.append({"file": os.path.basename(path), "line": node.lineno,
                          "reason": reason, "kind": kind})
        elif name in LOG_FNS:
            logs.append({"file": os.path.basename(path), "line": node.lineno})
    return skips, logs


def ops_pulse_volume():
    """Total firings per skip reason, summed over every ops_pulse line in today's logs."""
    vol = Counter()
    pat = re.compile(r'top_skip_reasons"\s*:\s*\{([^}]*)\}')
    kv = re.compile(r'"([^"]+)"\s*:\s*(\d+)')
    try:
        logs = [os.path.join(LOGDIR, f) for f in os.listdir(LOGDIR) if f.endswith(".log")]
    except OSError:
        return vol
    logs.sort(key=os.path.getmtime, reverse=True)
    for lp in logs[:2]:                       # newest couple of logs only
        try:
            with open(lp, errors="ignore") as fh:
                for line in fh:
                    m = pat.search(line)
                    if not m:
                        continue
                    for reason, n in kv.findall(m.group(1)):
                        vol[reason] += int(n)
        except OSError:
            continue
    return vol


def reject_log_reasons():
    seen = Counter()
    try:
        with open(REJECT, errors="ignore") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                r = str(d.get("reason") or d.get("skip_reason") or "")
                if r:
                    seen[r] += 1
    except OSError:
        pass
    return seen


def family(reason):
    """Coarse grouping so the unification has a work plan rather than a flat list of 191."""
    r = reason or "?"
    table = [
        ("lane state / cut", ("disabled_lane", "paused", "lane_state", "sit_out", "sitout")),
        ("edge floor", ("min_edge", "edge_below", "nonpositive_edge", "edge_")),
        ("bias / direction", ("neutral_bias", "htf", "bias", "quant", "direction", "side")),
        ("momentum confirm", ("momentum", "macd", "confirm", "rsi")),
        ("price band", ("price", "centered", "even", "band", "no_price")),
        ("timing / window", ("window", "late", "timing", "deadzone", "mins_left", "expiry")),
        ("sizing / kelly", ("kelly", "notional", "size", "exposure")),
        ("liquidity / book", ("spread", "depth", "book", "slippage", "liquidity")),
        ("ai / shadow", ("ai_", "shadow", "veto")),
        ("data quality", ("stale", "missing", "nan", "oracle", "feed", "warmup")),
    ]
    for label, keys in table:
        if any(k in r for k in keys):
            return label
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="also write machine-readable output here")
    args = ap.parse_args()

    files = []
    for d in (STRAT_DIR, os.path.join(REPO, "src")):
        if not os.path.isdir(d):
            continue
        for root, _dirs, fns in os.walk(d):
            for fn in fns:
                if fn.endswith(".py"):
                    files.append(os.path.join(root, fn))
    files = sorted(set(files))

    all_skips, all_logs = [], []
    for p in files:
        s, l = scan_file(p)
        all_skips += s
        all_logs += l

    logs_by_file = defaultdict(list)
    for l in all_logs:
        logs_by_file[l["file"]].append(l["line"])

    for s in all_skips:
        near = logs_by_file.get(s["file"], [])
        s["covered"] = any(abs(ln - s["line"]) <= PAIR_WINDOW for ln in near)

    vol = ops_pulse_volume()
    seen = reject_log_reasons()

    print("=== DECISION WRITE-POINT INVENTORY ===")
    print(f"  files scanned            {len(files)}")
    print(f"  _bump_skip sites         {len(all_skips)}")
    print(f"  decision-record calls    {len(all_logs)}")
    cov = sum(1 for s in all_skips if s["covered"])
    print(f"  sites with a log call within {PAIR_WINDOW} lines: {cov}/{len(all_skips)} "
          f"({cov / max(len(all_skips), 1) * 100:.0f}%)")
    print()

    # ── the number that matters: decisions lost, not sites ────────────────────
    print("  --- BY FIRING VOLUME (ops_pulse — LOWER BOUND, top-N per cycle only) ---")
    tot_vol = sum(vol.values())
    recorded = sum(n for r, n in vol.items() if seen.get(r))
    print(f"  skip firings OBSERVED         {tot_vol}   (true total is higher; see caveat)")
    print(f"  observed firings recorded     {recorded}")
    print(f"  observed firings UNRECORDED   {tot_vol - recorded}   "
          f"across {sum(1 for r in vol if not seen.get(r))} distinct reasons")
    print(f"  ⚠️ ratios here are DIRECTIONAL — top_skip_reasons is truncated per cycle, so")
    print(f"     rarely-top reasons are under-counted. Use this to RANK, not to quote %.")
    print()
    print(f"  reject-log rows available for cross-check: {sum(seen.values())}")
    if sum(seen.values()) < 5000:
        print("     ⚠️ SMALL — if the log rotated recently, 'zero rows' means 'not yet',")
        print("        not 'never recorded'. Re-run once it has rebuilt.")
    print()

    print("  --- BLIND SPOTS: fires live, ZERO rows in the reject log ---")
    blind = [(n, r) for r, n in vol.items() if not seen.get(r)]
    blind.sort(reverse=True)
    if not blind:
        print("    none — every firing reason reaches the reject log")
    for n, r in blind[:15]:
        print(f"    {n:7d}  {r:42s} [{family(r)}]")
    if len(blind) > 15:
        print(f"    ...and {len(blind) - 15} more")
    print()

    print("  --- COVERED: fires live AND recorded ---")
    ok = sorted(((n, r) for r, n in vol.items() if seen.get(r)), reverse=True)
    for n, r in ok[:8]:
        print(f"    {n:7d}  {r:42s} rows={seen[r]}")
    if not ok:
        print("    none")
    print()

    # ── work plan: silent sites grouped by family ────────────────────────────
    print(f"  --- SILENT SITES BY FAMILY (no log call within {PAIR_WINDOW} lines) ---")
    fam = defaultdict(list)
    for s in all_skips:
        if s["covered"]:
            continue
        fam[family(s["reason"])].append(s)
    for label in sorted(fam, key=lambda k: -len(fam[k])):
        sites = fam[label]
        byfile = Counter(x["file"] for x in sites)
        top = ", ".join(f"{f}×{c}" for f, c in byfile.most_common(3))
        print(f"    {label:22s} {len(sites):4d} sites   {top}")
    print()

    print("  --- reason kinds (affects how a unified writer must accept them) ---")
    for k, c in Counter(s["kind"] for s in all_skips).most_common():
        print(f"    {k:10s} {c}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"skips": all_skips, "logs": all_logs,
                       "volume": dict(vol), "reject_log_rows": dict(seen)}, fh, indent=2)
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
