#!/usr/bin/env python3
"""
claude_direction.py — write live trade-direction decisions and flip AI-drive mode.

This is the operator-selected "Claude decides, live" seam. The running bot reads the
override file fresh every scan (mtime-cached in src/analysis/direction_override.py), so
`set`/`clear` take effect IMMEDIATELY — no restart, no hot-reload dependency.

  # let Claude drive (still SHADOW until you enforce):
  python scripts/claude_direction.py mode claude
  python scripts/claude_direction.py enforce on          # <-- now overrides DRIVE trades

  # decide a side (asset[:tf] key; omit --tf for asset-wide; asset '*' = global default):
  python scripts/claude_direction.py set --asset hype_macro --tf 15m --side SHORT --conf 0.85 --ttl 900 --why "tape down, 1h bearish"
  python scripts/claude_direction.py set --asset bitcoin --side LONG --conf 0.7

  python scripts/claude_direction.py list          # show overrides + freshness + current mode
  python scripts/claude_direction.py clear --asset hype_macro --tf 15m
  python scripts/claude_direction.py clear --all
  python scripts/claude_direction.py mode quant     # back to pure quant (AI-drive off)

Sides: LONG / SHORT (or BUY_YES/BUY_NO, YES/NO, UP/DOWN). FLAT/NONE/SKIP => sit out.
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "settings.yaml")
DEFAULT_OVERRIDE = os.path.join(ROOT, "data", "runtime", "claude_direction_override.json")

VALID_SIDES = {
    "LONG": "LONG", "BUY_YES": "LONG", "YES": "LONG", "UP": "LONG",
    "SHORT": "SHORT", "BUY_NO": "SHORT", "NO": "SHORT", "DOWN": "SHORT",
    "FLAT": "FLAT", "NONE": "FLAT", "SKIP": "FLAT",
}


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _load(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _key(asset, tf):
    return f"{asset}:{tf}" if tf else asset


# ── config direction: block — surgical line edit (preserve the file exactly) ──
def _edit_direction_field(field, value):
    with open(CONFIG) as fh:
        lines = fh.readlines()
    in_block = False
    changed = False
    for i, ln in enumerate(lines):
        stripped = ln.rstrip("\n")
        if stripped == "direction:":
            in_block = True
            continue
        if in_block:
            # block ends at the next column-0, non-comment, non-blank line
            if ln and not ln[0].isspace() and not ln.lstrip().startswith("#"):
                break
            key = ln.split(":", 1)[0].strip()
            if key == field:
                indent = ln[: len(ln) - len(ln.lstrip())]
                # preserve any trailing inline comment on the line
                comment = ""
                if "#" in ln:
                    comment = "  #" + ln.split("#", 1)[1].rstrip("\n")
                lines[i] = f"{indent}{field}: {value}{comment}\n"
                changed = True
                break
    if not changed:
        print(f"ERROR: could not find '{field}' in the direction: block of {CONFIG}", file=sys.stderr)
        sys.exit(2)
    with open(CONFIG, "w") as fh:
        fh.writelines(lines)


def _read_direction_block():
    out = {}
    in_block = False
    try:
        with open(CONFIG) as fh:
            for ln in fh:
                if ln.rstrip("\n") == "direction:":
                    in_block = True
                    continue
                if in_block:
                    if ln and not ln[0].isspace() and not ln.lstrip().startswith("#"):
                        break
                    if ":" in ln and not ln.lstrip().startswith("#"):
                        k, v = ln.split(":", 1)
                        out[k.strip()] = v.split("#", 1)[0].strip()
    except Exception:
        pass
    return out


def cmd_set(a):
    side = VALID_SIDES.get(a.side.upper())
    if side is None:
        print(f"ERROR: bad --side {a.side!r}. Use LONG/SHORT/FLAT.", file=sys.stderr)
        sys.exit(2)
    path = a.file or DEFAULT_OVERRIDE
    data = _load(path)
    entry = {"side": side, "ts": int(time.time())}
    if a.conf is not None:
        entry["conf"] = a.conf
    if a.ttl is not None:
        entry["ttl"] = a.ttl
    if a.why:
        entry["why"] = a.why
    data[_key(a.asset, a.tf)] = entry
    _atomic_write(path, data)
    print(f"set {_key(a.asset, a.tf)} -> {json.dumps(entry)}")
    _warn_if_inert()


def cmd_clear(a):
    path = a.file or DEFAULT_OVERRIDE
    if a.all:
        _atomic_write(path, {})
        print("cleared ALL overrides")
        return
    data = _load(path)
    k = _key(a.asset, a.tf)
    if k in data:
        del data[k]
        _atomic_write(path, data)
        print(f"cleared {k}")
    else:
        print(f"(no entry {k})")


def cmd_list(a):
    path = a.file or DEFAULT_OVERRIDE
    data = _load(path)
    blk = _read_direction_block()
    now = int(time.time())
    print(f"config direction: mode={blk.get('mode')} enforce={blk.get('enforce')} "
          f"max_age_sec={blk.get('max_age_sec')} neutral={blk.get('override_when_quant_neutral')}")
    if blk.get("mode") == "quant":
        print("  -> AI-drive OFF (pure quant). `mode claude` to arm.")
    elif blk.get("enforce", "false") in ("false", "False"):
        print("  -> SHADOW: overrides logged as DIRECTION_OVERRIDE but NOT driving. `enforce on` to drive.")
    else:
        print("  -> LIVE: overrides are DRIVING trade sides.")
    print(f"override file: {path}")
    if not data:
        print("  (no overrides)")
    for k in sorted(data):
        e = data[k]
        age = now - int(e.get("ts", now)) if e.get("ts") else None
        ttl = e.get("ttl", blk.get("max_age_sec", 900))
        try:
            fresh = age is None or age <= float(ttl)
        except (TypeError, ValueError):
            fresh = True
        flag = "FRESH" if fresh else "STALE"
        print(f"  {k:22s} {e.get('side'):5s} conf={e.get('conf')} age={age}s [{flag}] {e.get('why','')}")


def cmd_mode(a):
    if a.value not in ("quant", "claude", "ai"):
        print("ERROR: mode must be quant|claude|ai", file=sys.stderr)
        sys.exit(2)
    _edit_direction_field("mode", a.value)
    print(f"direction.mode -> {a.value}")
    if a.value != "quant":
        blk = _read_direction_block()
        if blk.get("enforce", "false") in ("false", "False"):
            print("  NOTE: enforce=false (SHADOW). Run `enforce on` when you want it to DRIVE.")
    print("  NOTE: mode/enforce are config flags — confirm the bot picked them up "
          "(hot-reload or restart) with `list` against the live log.")


def cmd_enforce(a):
    val = "true" if a.value in ("on", "true", "1") else "false"
    _edit_direction_field("enforce", val)
    print(f"direction.enforce -> {val}")
    if val == "true":
        blk = _read_direction_block()
        if blk.get("mode") == "quant":
            print("  WARNING: mode=quant, so enforce has no effect. Run `mode claude` first.")


def _warn_if_inert():
    blk = _read_direction_block()
    if blk.get("mode") == "quant":
        print("  NOTE: mode=quant — this override is stored but INERT until `mode claude`.")
    elif blk.get("enforce", "false") in ("false", "False"):
        print("  NOTE: enforce=false (SHADOW) — override is logged but not driving yet.")


def main():
    p = argparse.ArgumentParser(description="Claude live direction overrides + AI-drive mode switch")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="set a direction override")
    s.add_argument("--asset", required=True, help="strategy key, e.g. hype_macro / bitcoin / '*'")
    s.add_argument("--tf", default=None, help="timeframe, e.g. 5m/15m/1h (omit = asset-wide)")
    s.add_argument("--side", required=True, help="LONG/SHORT/FLAT")
    s.add_argument("--conf", type=float, default=None)
    s.add_argument("--ttl", type=float, default=None, help="seconds this override stays valid")
    s.add_argument("--why", default="")
    s.add_argument("--file", default=None)
    s.set_defaults(func=cmd_set)

    c = sub.add_parser("clear", help="clear override(s)")
    c.add_argument("--asset", default=None)
    c.add_argument("--tf", default=None)
    c.add_argument("--all", action="store_true")
    c.add_argument("--file", default=None)
    c.set_defaults(func=cmd_clear)

    l = sub.add_parser("list", help="list overrides + current mode")
    l.add_argument("--file", default=None)
    l.set_defaults(func=cmd_list)

    m = sub.add_parser("mode", help="set direction.mode")
    m.add_argument("value", help="quant|claude|ai")
    m.set_defaults(func=cmd_mode)

    e = sub.add_parser("enforce", help="set direction.enforce")
    e.add_argument("value", help="on|off")
    e.set_defaults(func=cmd_enforce)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
