"""Loop 3 — self-healing supervisor: detect cold lanes / WR-collapse, auto-loosen
the safe whitelist, escalate the rest.

This is the "self-healing" layer that sits above:
  * Loop 1 — AI gating (per-trade, real-time; ``ai.decision_layer``)
  * Loop 2 — calibration recompute (``lane_thresholds`` / ``lane_posteriors``)

It watches *aggregate* conditions and either auto-applies a bounded, reversible,
loosen-only fix, or writes a structured escalation packet for a human / Codex.

Non-negotiable invariants (from CLAUDE.md + memory):
  * **Loosen-biased.** Auto-apply may only *loosen* (relax min_edge to revive a
    starved lane). Anything that would tighten, veto, resize, change exits, touch
    the AI gate, or add lanes is ESCALATED, never auto-applied.
  * **Ghost-validate, never backtest.** The only auto-apply path reuses
    ``performance_feedback.check_overtight`` — which already proves the relaxation
    admits +EV against ``rejected_candidates_settled.jsonl`` — so applied values
    live in the *consumer-correct* lane-id space and inherit the 0.70 floor.
  * **Runtime + TTL only.** Auto-applied overrides are in-memory with an expiry;
    they never edit ``settings.yaml``. Persistent changes go through escalation.

Lane-id note: ``trades.jsonl`` uses the 5-part live identity
(``strategy|window|side|bias|family``) while the runtime feedback consumer keys on
the 4-part ``strategy|window|side|regime``. We never hand-build the 4-part key —
auto-apply takes ``check_overtight``'s own rows and intersects with cold lanes on
``(strategy, window, side)`` only, sidestepping the mismatch entirely.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.analysis.ghost_calibration import DEFAULT_SETTLED_LOG  # noqa: F401  (era anchor / parity)
from src.execution.performance_feedback import check_overtight

logger = logging.getLogger(__name__)

# Repo root: src/analysis/self_healing.py -> parents[2]
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TRADES_LOG = _REPO_ROOT / "data" / "calibration" / "trades.jsonl"
DEFAULT_ESCALATION_DIR = _REPO_ROOT / "data" / "learning" / "escalations"
DEFAULT_ACTIONS_LOG = _REPO_ROOT / "data" / "learning" / "self_healing_actions.jsonl"

# In-config state keys (in-memory only, mirrors performance_feedback's _runtime_feedback).
_OVERRIDES_KEY = "_self_healing_overrides"   # {feedback_lane_id: {..., expires_at}}
_STATE_KEY = "_self_healing_state"           # {"escalated": {dedupe_key: iso}}

_GUARDRAILS = (
    "PHASE=calibration/data-gathering. NEVER tighten gates, raise min_edge, or "
    "narrow windows. Validate ONLY against data/calibration/rejected_candidates_"
    "settled.jsonl (ghost log); the backtester is known-broken — do not run or cite "
    "src/backtest/*. Ghosts cannot validate sizing/exits/AI-veto/new-lanes — if the "
    "fix needs those, stop and ask the user."
)


# ── helpers ──────────────────────────────────────────────────────────────────

def self_healing_enabled(config: Dict[str, Any]) -> bool:
    return bool((config.get("self_healing") or {}).get("enabled", False))


def _cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("self_healing") or {}


_jsonl_cache: Dict[str, Any] = {}


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    """2026-07-27 MEM-CHURN FIX: cache the parsed rows per-path keyed on file
    identity (mtime+size). The cold-lane cadence re-read+re-parsed the whole
    (growing) trades/decision logs every run — native-RSS churn. Same rows
    returned in the same order; cache invalidates whenever the file changes."""
    if not path.exists():
        return []
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    prev = _jsonl_cache.get(str(path))
    if prev is not None and prev[0] == key:
        return prev[1]
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    _jsonl_cache[str(path)] = (key, rows)
    return rows


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _lane_key(strategy: Any, window: Any, side: Any) -> Tuple[str, str, str]:
    return (
        str(strategy or "").strip().lower(),
        str(window or "").strip().lower(),
        str(side or "").strip().lower(),
    )


def _key_from_trade(row: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """(strategy, window, side) from the 5-part lane_id.

    The ``side`` field on a trade row is the *action* (BUY_YES/BUY_NO); the lane_id
    3rd part is the directional ``up``/``down`` used by check_overtight and the AI
    decision log, so we key off lane_id to stay in one namespace.
    """
    parts = str(row.get("lane_id") or "").split("|")
    if len(parts) < 3:
        return None
    return _lane_key(parts[0], parts[1], parts[2])


def _normalize_side(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"buy_yes", "yes", "long"}:
        return "up"
    if raw in {"buy_no", "no", "short"}:
        return "down"
    return raw


def _scope_set(config: Dict[str, Any], key: str) -> Optional[set[str]]:
    raw = (_cfg(config).get("scope") or {}).get(key)
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    vals = {
        _normalize_side(v) if key == "sides" else str(v or "").strip().lower()
        for v in raw
    }
    vals.discard("")
    return vals or None


def _scope_allows(
    config: Dict[str, Any],
    strategy: Any,
    window: Any = None,
    side: Any = None,
    family: Any = None,
    trigger: Any = None,
) -> bool:
    """True when a lane is inside the current self-healing review scope.

    The bot now carries inactive/experimental macro families alongside the
    operator-facing strategies. Scoping keeps the supervisor from creating noisy
    editor escalations for lanes that are not part of the active review surface.
    """

    strategies = _scope_set(config, "strategies")
    windows = _scope_set(config, "windows")
    sides = _scope_set(config, "sides")
    families = _scope_set(config, "families")
    triggers = _scope_set(config, "triggers")
    st = str(strategy or "").strip().lower()
    win = str(window or "").strip().lower()
    sd = _normalize_side(side)
    fam = str(family or "").strip().lower()
    trig = str(trigger or "").strip().lower()
    if strategies is not None and st not in strategies:
        return False
    if windows is not None and win and win not in windows:
        return False
    if sides is not None and sd and sd not in sides:
        return False
    if families is not None and fam and fam not in families:
        return False
    if triggers is not None and trig and trig not in triggers:
        return False
    return True


def _scope_allows_lane_id(
    config: Dict[str, Any],
    lane_id: Any,
    *,
    trigger: Any = None,
) -> bool:
    parts = str(lane_id or "").split("|")
    strategy = parts[0] if len(parts) > 0 else ""
    window = parts[1] if len(parts) > 1 else ""
    side = parts[2] if len(parts) > 2 else ""
    family = parts[4] if len(parts) > 4 else ""
    return _scope_allows(config, strategy, window, side, family, trigger)


def _side_from_action(action: str) -> str:
    a = str(action or "").strip().upper()
    if a == "BUY_YES":
        return "up"
    if a == "BUY_NO":
        return "down"
    return "unknown"


def _overtight_side(row: Dict[str, Any]) -> str:
    parts = str(row.get("lane_id") or "").split("|")
    return parts[2].strip().lower() if len(parts) >= 3 else "unknown"


# ── detectors ────────────────────────────────────────────────────────────────

def detect_cold_lanes(
    *,
    config: Dict[str, Any],
    now: datetime,
    trades_path: Optional[Path] = None,
    decision_log: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Lanes with no live entry for ``cold_hours``, each tagged with a cause.

    Cause attribution (decides auto-fix vs escalate):
      * ``ai_veto``       — decision_layer log shows candidates vetoed (not fail-open)
      * ``ai_unavailable``— vetoed because provider down on a fail-closed lane (ops)
      * ``quant_gate``    — check_overtight (ghost) has a +EV relaxation for the lane
      * ``structural``    — none of the above (no candidates / contradiction)
    Only ``quant_gate`` is auto-fixable; the rest escalate.
    """
    sc = _cfg(config).get("cold_lane") or {}
    if not bool(sc.get("enabled", True)):
        return []
    trades_path = Path(trades_path) if trades_path is not None else DEFAULT_TRADES_LOG
    cold_hours = float(sc.get("cold_hours", 24) or 24)
    cutoff = now - timedelta(hours=cold_hours)

    # Last live entry per (strategy, window, side).
    last_open: Dict[Tuple[str, str, str], datetime] = {}
    seen_keys: set = set()
    for row in _iter_jsonl(trades_path):
        key = _key_from_trade(row)
        if key is None:
            continue
        if not _scope_allows_lane_id(config, row.get("lane_id"), trigger="cold_lane"):
            continue
        seen_keys.add(key)
        ts = _parse_ts(row.get("opened_at") or row.get("ts"))
        if ts is None:
            continue
        if key not in last_open or ts > last_open[key]:
            last_open[key] = ts

    # AI-veto evidence per (strategy, window) from the decision-layer log.
    from src.analysis.ai_decision_settler import DECISION_LOG as _DECISION_LOG
    dpath = Path(decision_log) if decision_log is not None else _DECISION_LOG
    ai_vetoed: Dict[Tuple[str, str, str], Dict[str, int]] = {}
    for row in _iter_jsonl(dpath):
        ts = _parse_ts(row.get("ts_utc"))
        if ts is not None and ts < cutoff:
            continue
        side = _side_from_action(row.get("quant_action"))
        key = _lane_key(row.get("strategy"), row.get("window"), side)
        if not _scope_allows(config, key[0], key[1], key[2], trigger="cold_lane"):
            continue
        approved = row.get("approved")
        if approved is True:
            continue  # lane is firing via AI; not the cause
        if approved is False and not bool(row.get("fail_open")):
            slot = ai_vetoed.setdefault(key, {"vetoed": 0, "unavailable": 0})
            slot["vetoed"] += 1
            if "unavail" in str(row.get("reason") or "").lower() or "timeout" in str(row.get("reason") or "").lower():
                slot["unavailable"] += 1

    # Ghost-backed quant-gate relaxations available (consumer-correct lane ids).
    overtight_by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in _relaxed_overtight(config):
        k = _lane_key(row.get("strategy"), row.get("window"), _overtight_side(row))
        if not _scope_allows_lane_id(config, row.get("lane_id"), trigger="cold_lane"):
            continue
        overtight_by_key.setdefault(k, []).append(row)

    cold: List[Dict[str, Any]] = []
    for key in sorted(seen_keys):
        last = last_open.get(key)
        if last is not None and last >= cutoff:
            continue  # still trading
        age_h = (now - last).total_seconds() / 3600.0 if last else None
        strategy, window, side = key
        if window == "5m":
            # 5m never calls AI; cold 5m is always a quant/structural matter.
            pass
        veto = ai_vetoed.get(key)
        if veto and veto["vetoed"] > 0:
            cause = "ai_unavailable" if veto["unavailable"] >= veto["vetoed"] else "ai_veto"
            auto = False
        elif key in overtight_by_key:
            cause = "quant_gate"
            auto = True
        else:
            cause = "structural"
            auto = False
        cold.append(
            {
                "strategy": strategy,
                "window": window,
                "side": side,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "last_entry": last.isoformat() if last else None,
                "cause": cause,
                "auto_fixable": auto,
                "overtight": overtight_by_key.get(key, []),
                "ai_veto_evidence": veto,
            }
        )
    return cold


def detect_wr_collapse(
    *,
    config: Dict[str, Any],
    trades_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Per-lane recent WR dropping below the lane's own prior baseline.

    Always escalates (no auto-veto): Loop-2's lane_thresholds recompute already
    auto-vetoes chronic sub-floor lanes; this catches *collapse* earlier so a human
    can diagnose before the chronic veto fires.
    """
    wc = _cfg(config).get("wr_collapse") or {}
    if not bool(wc.get("enabled", True)):
        return []
    trades_path = Path(trades_path) if trades_path is not None else DEFAULT_TRADES_LOG
    window_n = int(wc.get("window_n", 30) or 30)
    min_sample = int(wc.get("min_sample", 25) or 25)
    collapse_delta = float(wc.get("collapse_delta", 0.15) or 0.15)

    # Ordered outcomes per full lane_id.
    seq: Dict[str, List[Tuple[datetime, bool]]] = {}
    for row in _iter_jsonl(trades_path):
        win = row.get("win")
        if not isinstance(win, bool):
            continue
        lane_id = str(row.get("lane_id") or "").strip()
        if not lane_id:
            continue
        if not _scope_allows_lane_id(config, lane_id, trigger="wr_collapse"):
            continue
        ts = _parse_ts(row.get("closed_at") or row.get("ts") or row.get("opened_at"))
        if ts is None:
            continue
        seq.setdefault(lane_id, []).append((ts, win))

    out: List[Dict[str, Any]] = []
    for lane_id, rows in seq.items():
        if len(rows) < window_n + min_sample:
            continue
        rows.sort(key=lambda r: r[0])
        recent = rows[-window_n:]
        baseline = rows[:-window_n]
        recent_wr = sum(1 for _, w in recent if w) / len(recent)
        base_wr = sum(1 for _, w in baseline if w) / len(baseline)
        if recent_wr < base_wr - collapse_delta:
            out.append(
                {
                    "lane_id": lane_id,
                    "recent_n": len(recent),
                    "recent_wr": round(recent_wr, 4),
                    "baseline_n": len(baseline),
                    "baseline_wr": round(base_wr, 4),
                    "drop": round(base_wr - recent_wr, 4),
                }
            )
    out.sort(key=lambda r: r["drop"], reverse=True)
    return out


# ── auto-apply (loosen-only, runtime + TTL) ──────────────────────────────────

def _relaxed_overtight(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """check_overtight at a cold-lane-relaxed sample bar, on a throwaway config copy.

    Reuses the proven loop so applied values stay in the consumer-correct id space
    and inherit its +EV guard and mult floor. We only lower the *sample* bars; the
    WR threshold and mult floor are untouched (still loosen-only, still +EV).
    """
    sc = _cfg(config).get("auto_apply") or {}
    tmp = copy.deepcopy(config)
    pf = tmp.setdefault("performance_feedback", {})
    pf["overtight_min_lane_sample"] = int(sc.get("cold_min_lane_sample", 12) or 12)
    pf["overtight_min_pass_sample"] = int(sc.get("cold_min_pass_sample", 8) or 8)
    # Floor stays whatever ops configured (default 0.70); never go below it here.
    floor = float(sc.get("min_edge_mult_floor", pf.get("overtight_min_edge_mult_floor", 0.70)))
    pf["overtight_min_edge_mult_floor"] = floor
    try:
        return check_overtight(tmp)
    except Exception as exc:  # noqa: BLE001 — telemetry only
        logger.warning("self_healing: relaxed check_overtight failed: %s", exc)
        return []


def reapply_active_overrides(config: Dict[str, Any], now: datetime) -> int:
    """Re-inject non-expired overrides into _runtime_feedback (perf-feedback clobbers
    it every refresh, so we re-apply each cycle) and prune expired ones."""
    store: Dict[str, Any] = config.setdefault(_OVERRIDES_KEY, {})
    if not store:
        return 0
    rf = config.setdefault("_runtime_feedback", {"enabled": True, "by_lane": {}, "by_strategy": {}})
    by_lane = rf.setdefault("by_lane", {})
    if rf.get("enabled") is False:
        rf["enabled"] = True
    live = 0
    for lane_id in list(store.keys()):
        rec = store[lane_id]
        exp = _parse_ts(rec.get("expires_at"))
        if exp is not None and exp <= now:
            del store[lane_id]
            continue
        # Never clobber a stronger perf-feedback loosen already present.
        existing = by_lane.get(lane_id)
        mult = float(rec.get("min_edge_mult", 1.0))
        if existing is not None and float(existing.get("min_edge_mult", 1.0)) <= mult:
            continue
        by_lane[lane_id] = {**rec.get("payload", {}), "min_edge_mult": mult, "source": "self_healing"}
        live += 1
    return live


def apply_auto_heal(
    config: Dict[str, Any],
    cold_lanes: List[Dict[str, Any]],
    *,
    now: datetime,
    actions_log: Path = DEFAULT_ACTIONS_LOG,
) -> List[Dict[str, Any]]:
    """Apply loosen-only min_edge overrides for cold-by-quant-gate lanes."""
    sc = _cfg(config).get("auto_apply") or {}
    if not bool(sc.get("enabled", True)):
        return []
    ttl_hours = float(sc.get("ttl_hours", 12) or 12)
    floor = float(sc.get("min_edge_mult_floor", 0.70) or 0.70)
    store = config.setdefault(_OVERRIDES_KEY, {})
    rf = config.setdefault("_runtime_feedback", {"enabled": True, "by_lane": {}, "by_strategy": {}})
    by_lane = rf.setdefault("by_lane", {})
    applied: List[Dict[str, Any]] = []

    for lane in cold_lanes:
        if not lane.get("auto_fixable"):
            continue
        for row in lane.get("overtight") or []:
            lane_id = str(row["lane_id"])
            mult = float(row["recommended_min_edge_mult"])
            # Loosen-only invariant — must never raise the gate.
            if mult > 1.0:
                logger.warning("self_healing: skipping non-loosen mult %.3f for %s", mult, lane_id)
                continue
            mult = max(floor, mult)
            # Don't override an equal/stronger perf-feedback loosen.
            existing = by_lane.get(lane_id)
            if existing is not None and existing.get("source") != "self_healing" \
                    and float(existing.get("min_edge_mult", 1.0)) <= mult:
                continue
            expires_at = (now + timedelta(hours=ttl_hours)).isoformat()
            payload = {
                "ghost_n": int(row["ghost_n"]),
                "ghost_win_rate": float(row["ghost_win_rate"]),
                "admitted_n": int(row["admitted_n"]),
                "admitted_win_rate": float(row["admitted_win_rate"]),
                "recommended_relax_delta": float(row["recommended_relax_delta"]),
                "verdict": str(row["verdict"]),
            }
            store[lane_id] = {
                "min_edge_mult": mult,
                "payload": payload,
                "applied_at": now.isoformat(),
                "expires_at": expires_at,
                "trigger": "cold_lane_quant_gate",
                "lane_age_hours": lane.get("age_hours"),
            }
            by_lane[lane_id] = {**payload, "min_edge_mult": mult, "source": "self_healing"}
            action = {
                "ts": now.isoformat(),
                "action": "auto_loosen_min_edge",
                "action_class": "auto_loosen",
                "severity": "medium",
                "editor_action": "monitor_runtime_override",
                "lane_id": lane_id,
                "min_edge_mult": mult,
                "expires_at": expires_at,
                **payload,
            }
            applied.append(action)
            _append_jsonl(actions_log, action)
    return applied


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")
    except OSError as exc:
        logger.warning("self_healing: append %s failed: %s", path, exc)


# ── escalation ───────────────────────────────────────────────────────────────

def _collapse_severity(lane: Dict[str, Any]) -> str:
    drop = float(lane.get("drop") or 0.0)
    recent_n = int(lane.get("recent_n") or 0)
    if drop >= 0.30 and recent_n >= 30:
        return "critical"
    if drop >= 0.20:
        return "high"
    return "medium"


def _triage_for_escalation(trigger: str, lane: Dict[str, Any]) -> Dict[str, Any]:
    """Classify editor work deterministically before any LLM summarizes it."""

    trigger = str(trigger or "").strip().lower()
    if trigger == "cold_lane:ai_unavailable":
        return {
            "action_class": "ops_alert",
            "severity": "high",
            "editor_action": "check_ai_provider_health",
            "auto_apply_allowed": False,
            "next_steps": [
                "Check /api/status AI fields and data/logs/ai_pipeline/decision_layer.jsonl for timeout/unavailable reasons.",
                "Do not widen quant gates until provider health is separated from strategy quality.",
            ],
        }
    if trigger == "cold_lane:ai_veto":
        return {
            "action_class": "ai_gate_review",
            "severity": "medium",
            "editor_action": "review_ai_veto_quality",
            "auto_apply_allowed": False,
            "next_steps": [
                "Review recent AI verdicts for the lane and compare vetoed candidates against settled ghost outcomes.",
                "If AI is opposing profitable ghosts, adjust AI gate/prompt behavior; do not tighten entry gates by default.",
            ],
        }
    if trigger == "cold_lane:structural":
        return {
            "action_class": "strategy_diagnosis",
            "severity": "low",
            "editor_action": "inspect_rejection_distribution",
            "auto_apply_allowed": False,
            "next_steps": [
                "Inspect rejected-candidate reasons for candidate starvation, window mismatch, oracle basis, and family gating.",
                "Only make a persistent config change if Ghost Lab or live journal evidence identifies a specific blocker.",
            ],
        }
    if trigger == "wr_collapse":
        return {
            "action_class": "performance_regression",
            "severity": _collapse_severity(lane),
            "editor_action": "compare_recent_vs_baseline_lane",
            "auto_apply_allowed": False,
            "next_steps": [
                "Compare the collapse window against the prior baseline by family, hour, oracle basis, and recent code/config changes.",
                "Let lane_thresholds handle chronic vetoes; use this alert to find the cause before disabling or tightening anything.",
            ],
        }
    return {
        "action_class": "manual_review",
        "severity": "medium",
        "editor_action": "review_packet",
        "auto_apply_allowed": False,
        "next_steps": [
            "Review the packet and validate against the appropriate source of truth before changing config."
        ],
    }

def build_escalation_packet(
    *,
    trigger: str,
    lane: Dict[str, Any],
    now: datetime,
    ghost_validatable: bool,
    diagnosis: str,
    proposed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    triage = _triage_for_escalation(trigger, lane)
    return {
        "generated_at": now.isoformat(),
        "trigger": trigger,
        **triage,
        "lane": lane,
        "diagnosis": diagnosis,
        "ghost_validatable": bool(ghost_validatable),
        "proposed_change": proposed or {},
        "recommendation_contract": (
            "Auto-apply is limited to ghost-backed min-edge loosening. This packet "
            "is editor review unless action_class is auto_loosen in the actions log."
        ),
        "guardrails": _GUARDRAILS,
        "notes": "Escalation only — requires human/Codex review. No auto-apply was performed for this item.",
    }


def write_escalation(packet: Dict[str, Any], *, queue_dir: Path = DEFAULT_ESCALATION_DIR) -> Optional[Path]:
    try:
        queue_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = queue_dir / f"escalation_{ts}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(packet, fh, indent=2)
        return path
    except OSError as exc:
        logger.warning("self_healing: escalation write failed: %s", exc)
        return None


def _dedupe_key(trigger: str, lane: Dict[str, Any], now: datetime) -> str:
    lid = lane.get("lane_id") or f"{lane.get('strategy')}|{lane.get('window')}|{lane.get('side')}"
    return f"{trigger}|{lid}|{now.strftime('%Y-%m-%d')}"


def _already_escalated(config: Dict[str, Any], key: str) -> bool:
    state = config.setdefault(_STATE_KEY, {})
    seen = state.setdefault("escalated", {})
    return key in seen


def _mark_escalated(config: Dict[str, Any], key: str, now: datetime) -> None:
    config.setdefault(_STATE_KEY, {}).setdefault("escalated", {})[key] = now.isoformat()


# ── orchestration ────────────────────────────────────────────────────────────

@dataclass
class SelfHealingResult:
    applied: List[Dict[str, Any]] = field(default_factory=list)
    escalations: List[Dict[str, Any]] = field(default_factory=list)
    notify_messages: List[str] = field(default_factory=list)
    reapplied: int = 0


class SelfHealingSupervisor:
    """Sync supervisor; invoked on the performance_feedback cadence from main."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def run(self, *, now: Optional[datetime] = None) -> SelfHealingResult:
        now = now or datetime.now(timezone.utc)
        result = SelfHealingResult()
        if not self_healing_enabled(self.config):
            return result

        # Auto-loosen rides on the performance_feedback consumer, which short-circuits
        # to 1.0 when that feature is off — so the override would silently no-op.
        if bool((_cfg(self.config).get("auto_apply") or {}).get("enabled", True)) and not bool(
            (self.config.get("performance_feedback") or {}).get("enabled", False)
        ):
            logger.warning(
                "self_healing: auto_apply enabled but performance_feedback is OFF — "
                "loosen overrides will not take effect (consumer returns 1.0). "
                "Enable performance_feedback or auto-loosen is a no-op."
            )

        # 1) Keep prior loosen overrides alive (perf-feedback clobbers _runtime_feedback).
        result.reapplied = reapply_active_overrides(self.config, now)

        esc_cfg = _cfg(self.config).get("escalation") or {}
        queue_dir = Path(esc_cfg.get("queue_dir") or DEFAULT_ESCALATION_DIR)
        if not queue_dir.is_absolute():
            queue_dir = _REPO_ROOT / queue_dir

        # 2) Cold lanes — auto-fix quant-gate, escalate the rest.
        cold = detect_cold_lanes(config=self.config, now=now)
        auto_cold = [c for c in cold if c.get("auto_fixable")]
        result.applied = apply_auto_heal(self.config, auto_cold, now=now)

        for lane in cold:
            if lane.get("auto_fixable"):
                continue
            cause = lane["cause"]
            key = _dedupe_key(f"cold_{cause}", lane, now)
            if _already_escalated(self.config, key):
                continue
            if cause == "ai_veto":
                diagnosis = (
                    f"Lane {lane['strategy']}|{lane['window']}|{lane['side']} cold "
                    f"{lane['age_hours']}h — AI decision layer is vetoing its candidates "
                    f"(evidence={lane['ai_veto_evidence']}). NOT ghost-validatable; do not "
                    f"widen quant gates. Review AI confidence/enforcement for this lane."
                )
                gv = False
            elif cause == "ai_unavailable":
                diagnosis = (
                    f"Lane {lane['strategy']}|{lane['window']}|{lane['side']} cold "
                    f"{lane['age_hours']}h — fail-closed lane starved by AI provider "
                    f"unavailability/timeout. Ops issue (provider health), not a gate change."
                )
                gv = False
            else:  # structural
                diagnosis = (
                    f"Lane {lane['strategy']}|{lane['window']}|{lane['side']} cold "
                    f"{lane['age_hours']}h with no ghost-backed +EV relaxation — likely "
                    f"structural (few candidates generated or a bias-vs-gate contradiction). "
                    f"Diagnose rejection-reason distribution for the current config era."
                )
                gv = (cause == "structural")
            packet = build_escalation_packet(
                trigger=f"cold_lane:{cause}", lane=lane, now=now,
                ghost_validatable=gv, diagnosis=diagnosis,
            )
            self._emit_escalation(packet, queue_dir, result)
            _mark_escalated(self.config, key, now)

        # 3) WR collapse — always escalate.
        for lane in detect_wr_collapse(config=self.config):
            key = _dedupe_key("wr_collapse", lane, now)
            if _already_escalated(self.config, key):
                continue
            diagnosis = (
                f"Lane {lane['lane_id']} WR collapse: recent {lane['recent_wr']:.0%} "
                f"(n={lane['recent_n']}) vs baseline {lane['baseline_wr']:.0%} "
                f"(n={lane['baseline_n']}), drop {lane['drop']:.0%}. Diagnose what changed; "
                f"Loop-2 lane_thresholds will auto-veto if it stays sub-floor."
            )
            packet = build_escalation_packet(
                trigger="wr_collapse", lane=lane, now=now,
                ghost_validatable=True, diagnosis=diagnosis,
            )
            self._emit_escalation(packet, queue_dir, result)
            _mark_escalated(self.config, key, now)

        if result.applied or result.escalations:
            logger.info(
                "self_healing: reapplied=%d auto_loosen=%d escalations=%d",
                result.reapplied, len(result.applied), len(result.escalations),
            )
        return result

    def _emit_escalation(
        self, packet: Dict[str, Any], queue_dir: Path, result: SelfHealingResult
    ) -> None:
        path = write_escalation(packet, queue_dir=queue_dir)
        packet["_path"] = str(path) if path else None
        result.escalations.append(packet)
        esc_cfg = _cfg(self.config).get("escalation") or {}
        if bool(esc_cfg.get("notify", True)):
            steps = packet.get("next_steps") or []
            first_step = steps[0] if steps else packet["diagnosis"]
            result.notify_messages.append(
                f"self-heal {packet['severity']} {packet['action_class']} "
                f"[{packet['trigger']}] editor_action={packet['editor_action']} "
                f"ghost_validatable={packet['ghost_validatable']}: {first_step}"
            )
        if bool(esc_cfg.get("codex_dispatch", False)) and packet.get("ghost_validatable"):
            self._maybe_dispatch_codex(packet)

    def _maybe_dispatch_codex(self, packet: Dict[str, Any]) -> None:
        """Opt-in, ghost-validatable-only Codex draft on a branch. Default off."""
        logger.info(
            "self_healing: codex_dispatch enabled — packet %s flagged for Codex (branch-only).",
            packet.get("_path"),
        )
        # Intentionally does not auto-merge; the dispatcher (see plan) drives codex exec
        # on a dedicated branch with _GUARDRAILS embedded. Left as a logged hook in v1.


def public_self_healing_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitized snapshot for the dashboard."""
    sc = _cfg(config)
    store = config.get(_OVERRIDES_KEY) or {}
    state = (config.get(_STATE_KEY) or {}).get("escalated") or {}
    return {
        "enabled": bool(sc.get("enabled", False)),
        "active_overrides": [
            {"lane_id": k, **{kk: vv for kk, vv in v.items() if kk != "payload"}}
            for k, v in store.items()
        ],
        "active_override_count": len(store),
        "escalations_today": len(state),
    }
