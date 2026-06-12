"""Decision-layer payload helpers for execution, skips, and journals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class DecisionSnapshot:
    """Stable execution context shared by entry and rejection records."""

    strategy: str
    market_id: str
    market_question: str
    action: Optional[str] = None
    direction: Optional[str] = None
    entry_leg: Optional[str] = None
    signal_reason: Optional[str] = None
    lane_meta: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_signal(
        cls,
        *,
        strategy: str,
        signal: Any,
        entry_leg: Optional[str],
        lane_meta: Mapping[str, Any],
    ) -> "DecisionSnapshot":
        return cls(
            strategy=strategy,
            market_id=str(getattr(signal, "market_id", "") or ""),
            market_question=str(getattr(signal, "market_question", "") or ""),
            action=getattr(signal, "action", None),
            direction=getattr(signal, "direction", None),
            entry_leg=entry_leg,
            signal_reason=getattr(signal, "reason", None),
            lane_meta=dict(lane_meta or {}),
        )

    @property
    def lane_id(self) -> str:
        return str(self.lane_meta.get("lane_id") or "").strip()

    def skip_extra(
        self,
        *,
        skip_reason: Optional[str] = None,
        dry_run: Optional[bool] = None,
        matched_rule: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = dict(self.lane_meta or {})
        if self.signal_reason:
            payload["signal_reason"] = self.signal_reason
        if skip_reason:
            payload["skip_reason"] = skip_reason
        if dry_run is not None:
            payload["dry_run"] = bool(dry_run)
        if matched_rule:
            payload["lane_rule_match"] = matched_rule
        return payload

    def entry_signal(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(fields or {})
        payload.update(dict(self.lane_meta or {}))
        return payload
