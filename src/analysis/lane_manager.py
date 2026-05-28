"""Config-backed lane state management for execution gating."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


class LaneManager:
    VALID_STATES = {"paper", "live", "paused"}

    def __init__(self, config: Dict[str, Any]):
        cfg = dict(config.get("lane_management") or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.execution_enforcement_enabled = bool(cfg.get("execution_enforcement_enabled", False))
        self.default_state = self._normalize_state(cfg.get("default_state", "paper"))
        rec_cfg = dict(cfg.get("recommendations") or {})
        self.rec_min_live_trades = int(rec_cfg.get("min_live_trades", 15) or 15)
        self.rec_min_live_win_rate = float(rec_cfg.get("min_live_win_rate", 0.54) or 0.54)
        self.rec_min_live_expectancy = float(rec_cfg.get("min_live_expectancy", 0.0) or 0.0)
        self.rec_max_live_gap = float(rec_cfg.get("max_live_gap", 0.04) or 0.04)
        self.rec_min_pause_trades = int(rec_cfg.get("min_pause_trades", 8) or 8)
        self.rec_max_pause_win_rate = float(rec_cfg.get("max_pause_win_rate", 0.40) or 0.40)
        self.rec_max_pause_expectancy = float(rec_cfg.get("max_pause_expectancy", -0.5) or -0.5)
        self.rec_min_pause_gap = float(rec_cfg.get("min_pause_gap", 0.08) or 0.08)
        self.rec_auto_pause_confirmation_trades = int(rec_cfg.get("auto_pause_confirmation_trades", 3) or 3)
        raw_states = cfg.get("states") or {}
        self.states: Dict[str, str] = {
            str(key).strip(): self._normalize_state(value)
            for key, value in raw_states.items()
            if str(key).strip()
        }
        raw_meta = cfg.get("state_meta") or {}
        self.state_meta: Dict[str, Dict[str, Any]] = {
            str(key).strip(): dict(value or {})
            for key, value in raw_meta.items()
            if str(key).strip() and isinstance(value, dict)
        }

    def _normalize_state(self, value: Any) -> str:
        state = str(value or "").strip().lower()
        return state if state in self.VALID_STATES else "paper"

    def get_lane_state(self, lane_id: str) -> Tuple[str, str]:
        lane = str(lane_id or "").strip()
        if not lane:
            return self.default_state, ""
        if lane in self.states:
            return self.states[lane], lane
        for key in sorted(self.states.keys(), key=len, reverse=True):
            if lane.startswith(key):
                return self.states[key], key
        return self.default_state, ""

    def can_execute(self, lane_id: str, *, dry_run: bool) -> tuple[bool, str, str, str]:
        state, matched_key = self.get_lane_state(lane_id)
        if not self.enabled:
            return True, "lane_management_disabled", state, matched_key
        if not self.execution_enforcement_enabled:
            return True, "lane_advisory_only", state, matched_key
        if state == "paused":
            return False, "lane_paused", state, matched_key
        return True, "lane_allowed", state, matched_key

    def get_lane_meta(self, lane_id: str) -> Tuple[Dict[str, Any], str]:
        lane = str(lane_id or "").strip()
        if not lane:
            return {}, ""
        if lane in self.state_meta:
            return dict(self.state_meta[lane]), lane
        for key in sorted(self.state_meta.keys(), key=len, reverse=True):
            if lane.startswith(key):
                return dict(self.state_meta[key]), key
        return {}, ""

    def recommend_state(self, metrics: Dict[str, Any]) -> tuple[str, List[str]]:
        trades = int(metrics.get("trades") or 0)
        win_rate = float(metrics.get("win_rate") or 0.0)
        expectancy = float(metrics.get("expectancy") or 0.0)
        gap = float(metrics.get("edge_realized_gap") or 0.0)
        reasons: List[str] = []

        if (
            trades >= self.rec_min_live_trades
            and win_rate >= self.rec_min_live_win_rate
            and expectancy >= self.rec_min_live_expectancy
            and gap <= self.rec_max_live_gap
        ):
            reasons.append(
                f"n={trades} WR={win_rate:.1%} exp={expectancy:+.2f} gap={gap:.3f} clears live bar"
            )
            return "live", reasons

        if (
            trades >= self.rec_min_pause_trades
            and win_rate <= self.rec_max_pause_win_rate
            and expectancy <= self.rec_max_pause_expectancy
            and gap >= self.rec_min_pause_gap
        ):
            reasons.append(
                f"n={trades} WR={win_rate:.1%} exp={expectancy:+.2f} gap={gap:.3f} suggests pause"
            )
            return "paused", reasons

        if trades < self.rec_min_live_trades:
            reasons.append(f"sample too small for live ({trades} < {self.rec_min_live_trades})")
        else:
            if win_rate < self.rec_min_live_win_rate:
                reasons.append(f"WR below live bar ({win_rate:.1%} < {self.rec_min_live_win_rate:.0%})")
            if expectancy < self.rec_min_live_expectancy:
                reasons.append(f"expectancy below live bar ({expectancy:+.2f})")
            if gap > self.rec_max_live_gap:
                reasons.append(f"gap above live bar ({gap:.3f} > {self.rec_max_live_gap:.3f})")
        return "paper", reasons

    def assess_lane(self, lane_id: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
        recommended_state, recommendation_reasons = self.recommend_state(metrics)
        effective_state, matched_rule = self.get_lane_state(lane_id)
        trades = int(metrics.get("trades") or 0)
        auto_pause_candidate = False
        auto_pause_confirmed = False
        auto_pause_confirmation_remaining = self.rec_auto_pause_confirmation_trades
        auto_pause_reason = ""

        if recommended_state == "paused" and effective_state == "live":
            over_min_pause = max(0, trades - self.rec_min_pause_trades)
            auto_pause_confirmation_remaining = max(0, self.rec_auto_pause_confirmation_trades - over_min_pause)
            auto_pause_candidate = True
            auto_pause_confirmed = auto_pause_confirmation_remaining == 0
            auto_pause_reason = (
                recommendation_reasons[0]
                if recommendation_reasons
                else "lane remains below pause bar while still live"
            )

        return {
            "recommended_state": recommended_state,
            "recommendation_reasons": recommendation_reasons,
            "effective_state": effective_state,
            "matched_rule": matched_rule,
            "auto_pause_candidate": auto_pause_candidate,
            "auto_pause_confirmed": auto_pause_confirmed,
            "auto_pause_confirmation_remaining": auto_pause_confirmation_remaining,
            "auto_pause_reason": auto_pause_reason,
        }
