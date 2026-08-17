#!/usr/bin/env python3
"""config_audit.py — fail loudly on the config defects that have actually bitten us.

WHY THIS EXISTS
───────────────
Every check below is here because it caused a real, costly wrong conclusion. This is not
a style linter; each rule has a receipt:

  DUP_KEY          At git HEAD, `bnb_macro.disable_buy_yes_15m` was set TWICE in the same
                   mapping (lines 2407 and 2620). yaml.safe_load silently keeps the LAST,
                   so line 2407 read as configuration while doing nothing. The 08-17 edits
                   removed 2620 — correctly, but on the strength of a hand-written comment
                   rather than a check. Verified: this detector flags that HEAD file and
                   passes the current one. Note the SAME key legitimately appears once per
                   strategy block; only same-mapping repeats are defects.
  STALE_KEY        CLAUDE.md lists 12 vestigial keys still present in settings.yaml. A key
                   that nothing reads looks like a live knob and invites "I set that".
  RESTART_DRIFT    `trading.window_watch` was enabled expecting hot-reload; it is not in
                   _HOT_RELOAD_TRADING_KEYS, so it produced 0 rows and looked like a code
                   bug. Restart-class keys edited under a running bot are invisible.
  LANE_CONTRADICT  `lane_management.states` pauses are enforced at exposure_manager, AFTER
                   candidate generation, and emit NO reject-log rows. A paused lane that
                   still takes entries is silent.
  EDGE_CONSISTENCY `term_risk.min_edge.SHORT_TERM: 0.05` vs the favorite policy's fixed
                   `0.02`. Crypto up/down is ALWAYS SHORT_TERM, so 0.02 < 0.05 was
                   unconditional: measured 763 signals -> 6 entries, bitcoin 547 -> 0.
  STALE_COMMENT    settings.yaml says "With side_policy: favorite below" while the value
                   50 lines down is `resolver`. That comment cost real reasoning time in
                   this session — the audit reads comments because humans do.

READ-ONLY. Imports nothing from the bot, writes nothing, cannot affect a trade.

EXIT CODES:  0 = clean or warnings only   ·   1 = at least one FAIL   ·   2 = audit broke
Use --strict to make warnings fail too.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import datetime as dt
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(REPO, "config", "settings.yaml")

FINDINGS = []          # (severity, code, message)


def add(sev, code, msg):
    FINDINGS.append((sev, code, msg))


# ── duplicate-key-detecting loader ────────────────────────────────────────────
def load_with_dup_detection(path):
    """Parse the YAML and report keys duplicated within the SAME mapping node.

    The same key name in DIFFERENT blocks is normal and correct here (every strategy has
    its own `min_edge`), so a naive whole-file grep is useless — the check has to be
    scoped to one mapping.
    """
    import yaml

    dups = []

    class DupLoader(yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):
        seen = {}
        for key_node, _val in node.value:
            try:
                key = loader.construct_object(key_node, deep=deep)
            except Exception:
                continue
            if not isinstance(key, (str, int, float, bool)):
                continue
            if key in seen:
                dups.append((str(key), seen[key], key_node.start_mark.line + 1))
            else:
                seen[key] = key_node.start_mark.line + 1
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    DupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    with open(path, encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=DupLoader)
    return data, dups


def flatten(obj, prefix=""):
    """Yield (dotted_path, value) for every leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        yield (prefix, obj)
    else:
        yield (prefix, obj)


# ── 1. duplicate keys ─────────────────────────────────────────────────────────
def check_dup_keys(dups):
    if not dups:
        add("PASS", "DUP_KEY", "no duplicate keys within any mapping")
        return
    by_key = defaultdict(list)
    for key, first, second in dups:
        by_key[key].append((first, second))
    for key, pairs in sorted(by_key.items()):
        locs = "; ".join(f"line {a} shadowed by line {b}" for a, b in pairs)
        add("FAIL", "DUP_KEY",
            f"`{key}` duplicated in the same block — YAML keeps the LAST value only ({locs})")


# ── 2. stale keys nothing reads ────────────────────────────────────────────────
KNOWN_VESTIGIAL = {
    "buy_no_ltf_override_max_btc_5m_pct", "buy_yes_ltf_override_min_btc_5m_pct",
    "require_btc_volatility_gate", "min_btc_move_pct_5m_for_lag_entries",
    "min_btc_move_pct_15m_for_lag_entries", "require_btc_catalyst_5m",
    "require_btc_catalyst_15m_when_unconfirmed", "btc_min_move_dollars_5m",
    "btc_min_move_dollars_15m", "btc_min_move_low_corr_threshold",
    "center_price_requires_catalyst", "neutral_bias_require_spike_or_lag",
    "neutral_macro_require_spike_or_lag",
}


def _referenced(name, src_blob):
    """Is this key read anywhere in src/ — literally OR by dynamic construction?

    Many real knobs are built at runtime, and the dynamic part sits on EITHER end:
      f"{tf}_buy_yes_bullish_floor_bump"   -> dynamic PREFIX, match on the suffix
      f"buy_no_min_no_price_{tf}"          -> dynamic SUFFIX, match on the prefix
    A literal-only search reported 93 "dead" keys. Stripping leading segments got it to 68,
    but `buy_no_min_no_price_15m` and `bull_regime_buy_no_max_yes_price_15m` were still
    false positives — both verifiably alive (3 and 2 src refs on their stems). So try both
    ends. Anything still unmatched is a genuine candidate.
    """
    if name in src_blob:
        return True
    parts = name.split("_")
    if len(parts) < 3:
        return False
    # dynamic prefix: drop leading segments
    for i in range(1, len(parts) - 1):
        stem = "_".join(parts[i:])
        if len(stem) >= 10 and stem in src_blob:
            return True
    # dynamic suffix: drop trailing segments
    for i in range(len(parts) - 1, 1, -1):
        stem = "_".join(parts[:i])
        if len(stem) >= 10 and stem in src_blob:
            return True
    return False


def check_stale_keys(cfg, src_blob):
    leaves = {p.split(".")[-1] for p, _ in flatten(cfg) if p}
    # Skip things that are obviously data, not knobs: lane ids, asset names, bare numbers.
    unread, vestigial = [], []
    for name in sorted(leaves):
        if not name or len(name) < 6 or "|" in name or name.isdigit():
            continue
        if _referenced(name, src_blob):
            continue
        (vestigial if name in KNOWN_VESTIGIAL else unread).append(name)
    if vestigial:
        add("WARN", "STALE_KEY",
            f"{len(vestigial)} known-vestigial key(s) still present (documented in CLAUDE.md, "
            f"safe to delete): {', '.join(vestigial[:8])}")
    if unread:
        add("WARN", "STALE_KEY",
            f"{len(unread)} key(s) with NO literal reference anywhere in src/ — either dead "
            f"or accessed dynamically, verify before trusting: {', '.join(unread[:12])}"
            + (f" (+{len(unread) - 12} more)" if len(unread) > 12 else ""))
    if not vestigial and not unread:
        add("PASS", "STALE_KEY", "every key is referenced in src/")


# ── 3. restart-class keys changed under a running bot ─────────────────────────
HOT_TOP = {"ai", "strategies", "exposure", "lane_management", "direction", "risk"}
HOT_TRADING = {
    "daily_loss_limit", "default_position_size", "exit_rules", "kelly_fraction",
    "max_days_to_resolution", "max_exposure_per_trade", "max_position_size",
    "min_hours_to_resolution", "slippage_guard",
}


def _bot_start():
    try:
        pid = subprocess.run(["pgrep", "-f", "src/main.py"], capture_output=True,
                             text=True).stdout.split()
        if not pid:
            return None, None
        pid = pid[0]
        out = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                             capture_output=True, text=True).stdout.strip()
        return pid, dt.datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
    except Exception:
        return None, None


def _changed_paths():
    """Top-level.second-level paths that differ between git HEAD and the live file."""
    try:
        head = subprocess.run(["git", "show", "HEAD:config/settings.yaml"],
                              cwd=REPO, capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    import yaml
    try:
        old = yaml.safe_load(head) or {}
        new = yaml.safe_load(open(CFG, encoding="utf-8")) or {}
    except Exception:
        return None
    oldf, newf = dict(flatten(old)), dict(flatten(new))
    return {p for p in set(oldf) | set(newf) if oldf.get(p) != newf.get(p)}


def _restart_class(paths):
    out = []
    for p in sorted(paths):
        parts = p.split(".")
        top = parts[0]
        if top in HOT_TOP:
            continue
        if top == "trading" and len(parts) > 1 and parts[1] in HOT_TRADING:
            continue
        out.append(p)
    return out


def _preboot_snapshot(started):
    """Newest config backup written BEFORE the bot booted.

    ⛔ WHY NOT FILE MTIME: the first version of this check compared settings.yaml's mtime
    against bot start and blamed EVERY restart-class diff on the latest write. That flagged
    `trading.journal_keep_events` as "not loaded" when it had been added ~9h before boot and
    was demonstrably live (_JOURNAL_KEEP_EVENTS == {'SKIP'}). One mtime cannot date
    individual keys. The repo keeps dozens of settings.yaml.bak_* — the newest one older
    than boot is a real snapshot of roughly what the process read.
    """
    if not started:
        return None
    cdir = os.path.dirname(CFG)
    best, best_mt = None, None
    try:
        for fn in os.listdir(cdir):
            if not fn.startswith("settings.yaml.") or fn == os.path.basename(CFG):
                continue
            fp = os.path.join(cdir, fn)
            try:
                mt = dt.datetime.fromtimestamp(os.path.getmtime(fp))
            except OSError:
                continue
            if mt < started and (best_mt is None or mt > best_mt):
                best, best_mt = fp, mt
    except OSError:
        return None
    return (best, best_mt) if best else None


def check_restart_drift():
    changed = _changed_paths()
    if changed is None:
        add("WARN", "RESTART_DRIFT", "could not diff config vs git HEAD — skipped")
        return

    rc_head = _restart_class(changed)
    if not changed:
        add("PASS", "RESTART_DRIFT", "live config matches git HEAD")
    elif not rc_head:
        add("PASS", "RESTART_DRIFT",
            f"{len(changed)} key(s) differ from HEAD, all hot-reloadable")
    else:
        add("WARN", "RESTART_DRIFT",
            f"{len(rc_head)} restart-class key(s) differ from git HEAD (uncommitted, but may "
            f"predate boot): " + ", ".join(rc_head[:6]))

    # The question that actually matters: changed SINCE THE PROCESS BOOTED.
    pid, started = _bot_start()
    if not (pid and started):
        add("WARN", "RESTART_DRIFT", "no running bot — cannot check post-boot drift")
        return
    snap = _preboot_snapshot(started)
    if not snap:
        add("WARN", "RESTART_DRIFT",
            f"bot pid {pid} started {started:%m-%d %H:%M} but no pre-boot settings backup "
            f"exists to diff against — post-boot drift UNKNOWN")
        return
    snap_path, snap_mt = snap

    # ⛔ THE DECIDING FACT: if the live file has not been written since boot, nothing can
    # be stale — certain, no inference needed.
    try:
        live_mt = dt.datetime.fromtimestamp(os.path.getmtime(CFG))
    except OSError:
        live_mt = None
    if live_mt and live_mt <= started:
        add("PASS", "RESTART_DRIFT",
            f"settings.yaml last written {live_mt:%m-%d %H:%M}, before boot "
            f"{started:%m-%d %H:%M} — the process has every key on disk")
        return

    import yaml
    try:
        old = dict(flatten(yaml.safe_load(open(snap_path, encoding="utf-8")) or {}))
        new = dict(flatten(yaml.safe_load(open(CFG, encoding="utf-8")) or {}))
    except Exception:
        add("WARN", "RESTART_DRIFT", f"could not parse snapshot {os.path.basename(snap_path)}")
        return
    rc_candidates = _restart_class(
        {p for p in set(old) | set(new) if old.get(p) != new.get(p)}
    )
    if not rc_candidates:
        add("PASS", "RESTART_DRIFT",
            f"config written after boot but no restart-class key differs from the nearest "
            f"pre-boot snapshot ({os.path.basename(snap_path)})")
        return

    # ⚠️ NOT a FAIL. The snapshot is the nearest backup OLDER than boot, which can predate
    # boot by hours — any change made in that gap WAS loaded. This check produced exactly
    # one false FAIL that way (`trading.journal_keep_events`, added ~9h pre-boot and
    # verified live in the process). A wrong FAIL is how an audit earns being ignored, so
    # report the ambiguity instead of asserting.
    gap_h = (started - snap_mt).total_seconds() / 3600.0
    add("WARN", "RESTART_DRIFT",
        f"{len(rc_candidates)} restart-class key(s) MIGHT be stale in pid {pid}: "
        + ", ".join(rc_candidates[:6])
        + (f" (+{len(rc_candidates) - 6} more)" if len(rc_candidates) > 6 else "")
        + f" — UNPROVEN: nearest pre-boot snapshot is {gap_h:.1f}h older than boot, so "
          f"changes in that gap were loaded. Key-level proof needs the bot to dump its "
          f"EFFECTIVE config at startup; nothing writes one today.")


# ── 4. paused lanes that still took entries ──────────────────────────────────
def check_lane_contradictions(cfg):
    states = ((cfg.get("lane_management") or {}).get("states") or {})
    paused = {k for k, v in states.items() if str(v).lower() == "paused"}
    if not paused:
        add("PASS", "LANE_CONTRADICT", "no paused lanes configured")
        return
    pid, started = _bot_start()
    if not started:
        add("WARN", "LANE_CONTRADICT",
            f"{len(paused)} lane(s) paused but no running bot to check entries against")
        return
    cutoff = started.astimezone().isoformat()
    tpath = os.path.join(REPO, "data", "calibration", "trades.jsonl")
    offenders = defaultdict(int)
    try:
        with open(tpath, errors="ignore") as fh:
            for line in fh:
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if str(t.get("opened_at") or "") < cutoff:
                    continue
                lane = (f"{t.get('strategy')}|{t.get('window')}|"
                        f"{'up' if t.get('action') == 'BUY_YES' else 'down'}")
                if lane in paused:
                    offenders[lane] += 1
    except OSError:
        add("WARN", "LANE_CONTRADICT", "trades.jsonl unreadable — skipped")
        return
    if offenders:
        add("FAIL", "LANE_CONTRADICT",
            "PAUSED lane(s) took entries after the bot started (pause not enforced): "
            + ", ".join(f"{k} n={v}" for k, v in sorted(offenders.items())))
    else:
        add("PASS", "LANE_CONTRADICT",
            f"{len(paused)} paused lane(s), 0 entries since bot start — enforcement holding")


# ── 5. edge / price-band consistency ─────────────────────────────────────────
def check_edge_consistency(cfg):
    d = cfg.get("direction") or {}
    policy = str(d.get("side_policy", "resolver") or "resolver").lower()
    flat = d.get("side_policy_flat_edge")
    band = d.get("side_policy_price_band")
    dead = d.get("side_policy_deadband")
    tr = ((cfg.get("term_risk") or {}).get("min_edge") or {})
    short_bar = tr.get("SHORT_TERM")

    # THE 763->6 defect. Only bites under `favorite`, where edge is pinned to flat_edge.
    if policy == "favorite" and flat is not None and short_bar is not None:
        if float(flat) < float(short_bar):
            add("FAIL", "EDGE_CONSISTENCY",
                f"side_policy=favorite pins edge to {flat}, but term_risk.min_edge."
                f"SHORT_TERM={short_bar} and crypto up/down is ALWAYS SHORT_TERM — every "
                f"favorite signal dies before an order (measured 763 signals -> 6 entries)")
        else:
            add("PASS", "EDGE_CONSISTENCY",
                f"favorite flat_edge {flat} >= SHORT_TERM bar {short_bar}")
    elif policy != "favorite":
        add("PASS", "EDGE_CONSISTENCY",
            f"side_policy={policy} — flat_edge/term_risk conflict dormant (favorite path off)")

    # a resolver-exempt list only means something under `favorite`
    lanes = d.get("side_policy_resolver_lanes") or []
    if policy != "favorite" and lanes:
        add("WARN", "EDGE_CONSISTENCY",
            f"side_policy={policy} but side_policy_resolver_lanes has {len(lanes)} entry/entries "
            f"— that list is the FAVORITE-mode exemption set and is inert here")

    if isinstance(band, (list, tuple)) and len(band) == 2:
        lo, hi = float(band[0]), float(band[1])
        if not (0.0 < lo < hi < 1.0):
            add("FAIL", "EDGE_CONSISTENCY",
                f"side_policy_price_band {band} is not a valid 0<lo<hi<1 interval")
        elif dead is not None and float(dead) >= (hi - lo) / 2.0:
            add("FAIL", "EDGE_CONSISTENCY",
                f"side_policy_deadband {dead} >= half the price band width "
                f"({(hi - lo) / 2.0:.3f}) — the band admits nothing")

    for name, val in flatten(cfg):
        if name.endswith("min_edge") or ".min_edge." in name:
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if v < 0 or v >= 1.0:
                add("FAIL", "EDGE_CONSISTENCY", f"{name} = {v} is outside (0,1)")


# ── 6. comments that contradict the value below them ─────────────────────────
def check_stale_comments(cfg):
    """Humans read comments. A comment asserting a value that is no longer true is a
    live trap — this session lost real time to `# With side_policy: favorite below` sitting
    above `side_policy: resolver`."""
    scalars = {}
    for p, v in flatten(cfg):
        if isinstance(v, (str, int, float, bool)) and v is not None:
            leaf = p.split(".")[-1]
            # only distinctive knob names, else "enabled: true" matches everything
            if len(leaf) >= 10 and "_" in leaf:
                scalars.setdefault(leaf, set()).add(str(v).lower())
    pat = re.compile(r"([a-z][a-z0-9_]{9,}):\s*([A-Za-z0-9_.\-]+)")
    hits = []
    try:
        with open(CFG, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                s = line.strip()
                if not s.startswith("#"):
                    continue
                for key, claimed in pat.findall(s):
                    # strip sentence punctuation the regex swallows, else `true.` != `true`
                    claimed_clean = claimed.strip(".,;:)").lower()
                    if not claimed_clean:
                        continue
                    vals = scalars.get(key)
                    if vals and claimed_clean not in vals:
                        hits.append((i, key, claimed_clean, sorted(vals)))
    except OSError:
        return
    # dedupe on (key, claimed) so one repeated boilerplate comment is not 20 findings
    seen, uniq = set(), []
    for i, key, claimed, vals in hits:
        if (key, claimed) in seen:
            continue
        seen.add((key, claimed))
        uniq.append((i, key, claimed, vals))
    if uniq:
        for i, key, claimed, vals in uniq[:8]:
            add("WARN", "STALE_COMMENT",
                f"line {i}: comment claims `{key}: {claimed}` but the live value is "
                f"{'/'.join(vals)}")
        if len(uniq) > 8:
            add("WARN", "STALE_COMMENT", f"...and {len(uniq) - 8} more contradicted comment(s)")
    else:
        add("PASS", "STALE_COMMENT", "no comment contradicts its key's live value")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    ap.add_argument("--quiet", action="store_true", help="only print WARN/FAIL")
    args = ap.parse_args()

    try:
        cfg, dups = load_with_dup_detection(CFG)
    except Exception as e:
        print(f"config_audit: CANNOT PARSE {CFG}: {type(e).__name__}: {e}")
        return 2

    src_blob = ""
    for root, _dirs, files in os.walk(os.path.join(REPO, "src")):
        for fn in files:
            if fn.endswith(".py"):
                try:
                    with open(os.path.join(root, fn), errors="ignore") as fh:
                        src_blob += fh.read()
                except OSError:
                    pass

    check_dup_keys(dups)
    check_stale_keys(cfg, src_blob)
    check_restart_drift()
    check_lane_contradictions(cfg)
    check_edge_consistency(cfg)
    check_stale_comments(cfg)

    icon = {"FAIL": "🔴 FAIL", "WARN": "🟡 WARN", "PASS": "✅ PASS"}
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    print("=== CONFIG AUDIT — config/settings.yaml ===")
    for sev, code, msg in sorted(FINDINGS, key=lambda x: (order[x[0]], x[1])):
        if args.quiet and sev == "PASS":
            continue
        print(f"  {icon[sev]}  {code:17s} {msg}")
    fails = sum(1 for s, _, _ in FINDINGS if s == "FAIL")
    warns = sum(1 for s, _, _ in FINDINGS if s == "WARN")
    print(f"\n  {fails} FAIL · {warns} WARN · "
          f"{sum(1 for s, _, _ in FINDINGS if s == 'PASS')} PASS")
    if fails or (args.strict and warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
