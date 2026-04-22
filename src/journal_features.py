"""
Structured timestamps and calendar features for trade journal `extra` blobs.

Used for pattern mining (hour-of-day, hold time, time-to-expiry) without new deps
beyond stdlib zoneinfo (Python 3.9+).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

PT_TZ = "America/Los_Angeles"


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def enrich_entry_extra(
    extra: Optional[dict[str, Any]],
    *,
    market_end_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Merge signal `extra` with entry-time UTC/PT fields and optional time-to-end."""
    out = dict(extra or {})
    t = _as_utc(now or datetime.now(timezone.utc))
    if out.get("hour_utc") is None:
        out["hour_utc"] = t.hour
    out.setdefault("ts_utc", t.strftime("%Y-%m-%dT%H:%M:%S") + "Z")
    out.setdefault("dow_utc", t.weekday())
    if ZoneInfo is not None:
        la = t.astimezone(ZoneInfo(PT_TZ))
        out.setdefault("hour_pt", la.hour)
        out.setdefault("dow_pt", la.weekday())
    if market_end_at is not None:
        try:
            me = _as_utc(market_end_at)
            out["minutes_to_market_end"] = int((me - t).total_seconds() / 60)
        except Exception:
            pass
    return out


def enrich_exit_extra(
    extra: dict[str, Any],
    opened_at_str: Optional[str],
    *,
    exit_ts: Optional[datetime] = None,
) -> dict[str, Any]:
    """Add exit-time hours, hold duration, and stable entry hour aliases."""
    out = dict(extra)
    hu = out.get("hour_utc")
    if hu is not None:
        out.setdefault("hour_utc_entry", int(hu))
    t = _as_utc(exit_ts or datetime.now(timezone.utc))
    out["exit_ts_utc"] = t.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    out["hour_utc_exit"] = t.hour
    out["dow_utc_exit"] = t.weekday()
    if ZoneInfo is not None:
        la = t.astimezone(ZoneInfo(PT_TZ))
        out["hour_pt_exit"] = la.hour
        out["dow_pt_exit"] = la.weekday()
    if opened_at_str:
        try:
            odt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
            odt = _as_utc(odt)
            out["hold_seconds"] = max(0, int((t - odt).total_seconds()))
            out.setdefault("opened_at", opened_at_str)
        except Exception:
            pass
    return out


def hold_bucket(seconds: Optional[int]) -> Optional[str]:
    """Coarse hold-time bucket for segmentation."""
    if seconds is None:
        return None
    if seconds < 300:
        return "0-5m"
    if seconds < 900:
        return "5-15m"
    if seconds < 3600:
        return "15-60m"
    return "60m+"
