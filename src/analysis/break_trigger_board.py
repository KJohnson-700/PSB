"""Generate data/dashboard/break_triggers.json — the DEFINITIONS the dashboard
Break-Trigger Board (/api/triggers) needs.

The dashboard endpoint (_triggers_payload_sync in server.py) already computes each
lane's LIVE closed/net/WR/ride0 from the running session's exits and colours the
card (safe/watch/fire) against per-trigger thresholds. It just reads its trigger
LIST from data/dashboard/break_triggers.json — and nothing ever wrote that file, so
the card has been blank since it was added (2026-07-16). This module writes it.

One trigger per lane that has traded in the current session, keyed
``strategy|window|direction`` (e.g. ``eth_macro|1h|up``; matches the endpoint's
lane3 join = first 3 parts of lane_id). Thresholds are the
per-lane break condition (net loss / low WR / ride-to-zero). SHADOW-safe: this only
produces a VISUALISATION feed; it never cuts anything. Whether the breaker actually
enforces is a separate switch (lane_management.execution_enforcement_enabled).

Usage:
    python -m src.analysis.break_trigger_board            # write the file
    python -m src.analysis.break_trigger_board --print
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"
OUT = DATA / "dashboard" / "break_triggers.json"
PAPER_DIR = DATA / "paper_trades"

# per-lane break thresholds (config trading.break_triggers overrides these)
DEFAULTS = {
    "thresh_net": -12.0,   # lane net <= this $ -> fire
    "thresh_wr": 33.0,     # WR% < this (with >= min_closed) -> fire
    "thresh_ride0": 2,     # this many ride-to-zero losers -> fire
    "min_closed": 6,
}


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = dict(DEFAULTS)
    if config:
        blk = ((config.get("trading") or {}).get("break_triggers")) or {}
        for k, v in (blk or {}).items():
            if k in c and v is not None:
                c[k] = v
    return c


def _current_session_dir() -> Optional[Path]:
    """Newest paper session dir that has entries (mirrors the dashboard's resolution)."""
    if not PAPER_DIR.exists():
        return None
    dirs = [d for d in PAPER_DIR.iterdir() if d.is_dir() and (d / "entries.jsonl").exists()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: (d / "entries.jsonl").stat().st_mtime)


def _active_lanes(session_dir: Path) -> List[str]:
    """lane3 keys (strategy|window|up|down) that have an ENTRY in this session."""
    lanes: Dict[str, int] = {}
    try:
        with open(session_dir / "entries.jsonl") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("event") != "ENTRY":
                    continue
                lid = (d.get("extra") or {}).get("lane_id") or ""
                l3 = "|".join(lid.split("|")[:3])
                if l3:
                    lanes[l3] = lanes.get(l3, 0) + 1
    except OSError:
        pass
    # most-active first (the card renders in list order)
    return [k for k, _ in sorted(lanes.items(), key=lambda x: -x[1])]


def build(config: Optional[Dict[str, Any]] = None, session_dir: Optional[Any] = None) -> Dict[str, Any]:
    c = _cfg(config)
    # Prefer the caller's resolved session (the dashboard passes its canonical
    # _current_session_dir_str) so build() and the endpoint never disagree on which
    # session is current — otherwise the self-heal would thrash every cache expiry.
    sd = Path(session_dir) if session_dir else _current_session_dir()
    lanes = _active_lanes(sd) if sd else []
    enforce_note = "cut lane"  # display hint; actual enforcement is a separate switch
    triggers = []
    for lane in lanes:
        parts = lane.split("|")
        label = ("%s %s %s" % (parts[0].replace("_macro", "").replace("bitcoin", "btc"),
                               parts[1], parts[2])).upper() if len(parts) == 3 else lane.upper()
        triggers.append({
            "id": lane,
            "label": label,
            "lane": lane,
            "cond": "net<=$%.0f | WR<%.0f%%@%d | ride0>=%d" % (
                c["thresh_net"], c["thresh_wr"], int(c["min_closed"]), int(c["thresh_ride0"])),
            "action": enforce_note,
            "thresh_net": c["thresh_net"],
            "thresh_wr": c["thresh_wr"],
            "thresh_ride0": int(c["thresh_ride0"]),
            "min_closed": int(c["min_closed"]),
            "since": None,  # count all closes in the current session
        })
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "session": sd.name if sd else None,
        "triggers": triggers,
    }


def write(payload: Dict[str, Any]) -> None:
    import os as _os
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # atomic write: a concurrent /api/triggers reader must never see a partial file
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    _os.replace(tmp, OUT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args()
    import yaml
    cfg_path = ROOT / "config" / "settings.yaml"
    config = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    payload = build(config)
    write(payload)
    print("Wrote %s — %d triggers (session=%s)" % (OUT, len(payload["triggers"]), payload["session"]))
    if args.do_print:
        for t in payload["triggers"]:
            print("  %-26s %s" % (t["lane"], t["cond"]))


if __name__ == "__main__":
    main()
