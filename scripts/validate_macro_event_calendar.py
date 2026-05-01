#!/usr/bin/env python3
"""
Validate BTC macro event calendar timestamps in config/settings.yaml.

Checks:
- Event datetime is valid ISO8601 UTC.
- Known event types map to expected America/New_York release times:
  - CPI / NFP (Employment Situation): 08:30 ET
  - FOMC Rate Decision: 14:00 ET

Usage:
  python3 scripts/validate_macro_event_calendar.py
  python3 scripts/validate_macro_event_calendar.py --config config/settings.yaml
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ExpectedTime:
    hour: int
    minute: int
    label: str


def _classify_event(name: str) -> Optional[ExpectedTime]:
    upper = (name or "").upper()
    if "FOMC" in upper:
        return ExpectedTime(14, 0, "FOMC")
    if "CPI" in upper:
        return ExpectedTime(8, 30, "CPI")
    if "NFP" in upper or "EMPLOYMENT SITUATION" in upper:
        return ExpectedTime(8, 30, "NFP")
    return None


def _parse_utc(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_events(config_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    if not config_path.exists():
        return [], [f"Config not found: {config_path}"]
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        return [], [f"Failed to load config: {exc}"]

    events = (
        cfg.get("strategies", {})
        .get("bitcoin", {})
        .get("macro_event_calendar_utc", [])
    )
    if not isinstance(events, list):
        return [], ["strategies.bitcoin.macro_event_calendar_utc must be a list"]
    return events, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate BTC macro event calendar timestamps.")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config" / "settings.yaml"),
        help="Path to settings.yaml",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    events, load_errors = _load_events(config_path)
    if load_errors:
        for err in load_errors:
            print(f"ERROR: {err}")
        return 2

    if not events:
        print("WARNING: macro_event_calendar_utc is empty.")
        return 1

    errors: List[str] = []
    warnings: List[str] = []
    checked = 0

    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"[{idx}] event must be an object")
            continue

        name = str(event.get("name") or "").strip()
        ts = event.get("datetime_utc")
        if not name:
            errors.append(f"[{idx}] missing 'name'")
            continue
        if not ts:
            errors.append(f"[{idx}] {name}: missing 'datetime_utc'")
            continue

        dt_utc = _parse_utc(str(ts))
        if dt_utc is None:
            errors.append(f"[{idx}] {name}: invalid datetime_utc '{ts}'")
            continue

        checked += 1
        expected = _classify_event(name)
        if expected is None:
            warnings.append(f"[{idx}] {name}: unknown event type (time check skipped)")
            continue

        dt_ny = dt_utc.astimezone(NY_TZ)
        if (dt_ny.hour, dt_ny.minute) != (expected.hour, expected.minute):
            errors.append(
                f"[{idx}] {name}: NY time is {dt_ny:%Y-%m-%d %H:%M %Z}, expected "
                f"{expected.hour:02d}:{expected.minute:02d} ET for {expected.label}"
            )

    print(
        f"Checked {checked} event timestamp(s): "
        f"{len(errors)} error(s), {len(warnings)} warning(s)."
    )
    for warn in warnings:
        print(f"WARNING: {warn}")
    for err in errors:
        print(f"ERROR: {err}")

    if errors:
        return 2
    if warnings:
        return 1
    print("OK: Macro event calendar timestamps match expected ET release times.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

