# PSB Gating Audit — Claude Code Handoff
**Date:** 2026-05-29
**Status:** READY FOR implementation
**Priority order:** 1 → 2 → 3 → 4 → 5

---

## Context

After recent config changes and refactoring, AI code editors (Claude Code, Codex) cannot reliably determine which config values are current vs legacy, which code paths exist vs were removed, or which math is correct vs stale. The root cause is a combination of broken feedback loops and missing existence assertions.

**Session history confirms the problem:**
- May 28-29 sessions (`test_20260529_014429`, `test_20260529_022925`): **0 entries, 0 exits** — bot is almost completely idle
- May 21 session: `updown_stop_loss` caused 66% of exits (-$125.60 total) — exit logic fires too early
- May 18 ghost audit: `edge_above_cap` BTC SHORT trades (65% WR) never fired — highest conviction signals blocked
- May 28: DOGE oracle broken (edge=0.0 on all oracle_basis_block), 7 DOGE signals, 0 trades

---

## Fix 1: Filter null realized_pct from ghost aggregations
**File:** `src/analysis/ghost_calibration.py` (or `tools/ghost_gate_report.py`)

**Root cause:** Backfilled ghost rows have `null` yes_price/no_price → `realized_pct: null`. The `_as_float()` helper converts `None` to `0.0` via `or 0.0`. This means every backfilled ghost without prices is counted as a zero-return trade, not unknown.

**Impact:** Every lane WR, net_gate_value, and ghost calibration metric is wrong. AI editors reading this data for "what the math says" get garbage.

**Fix:** In `_econ_metrics()`, filter rows where `realized_pct is None` before aggregating:

```python
def _econ_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Filter out rows with null realized_pct — they are unknown outcomes, not zeros
    valid = [r for r in rows if _as_float(r.get("realized_pct")) is not None]
    if not valid:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_realized_pct": 0.0, "total_realized_pct": 0.0,
                "missed_ev_pct": 0.0, "protected_loss_pct": 0.0,
                "net_gate_value_pct": 0.0,
                "win_rate_ci_low": 0.0, "win_rate_ci_high": 0.0}
    realized = [_as_float(r.get("realized_pct")) or 0.0 for r in valid]
    wins = sum(1 for r in valid if bool(r.get("win")) is True)
    losses = sum(1 for r in valid if bool(r.get("win")) is False)
    n = len(valid)
    # ... rest unchanged
```

Also update `aggregate_lanes()`, `aggregate_gates()`, and every aggregation function that calls `_econ_metrics()` to pass only valid rows.

**Verification:** After fix, re-run `ghost_gate_report.py` and confirm null rows are excluded from counts. Compare n totals before and after — the delta shows how many polluted rows existed.

---

## Fix 2: Add lane_min_edge to REJECTED_COPY_FIELDS
**File:** `src/analysis/ghost_calibration.py`

**Root cause:** `REJECTED_COPY_FIELDS` whitelist does not include `lane_min_edge` or the actual config value that caused the lane_min_edge rejection. The `stage` taxonomy defines `STAGE_LANE_MIN_EDGE = "lane_min_edge"` but the parameter values aren't copied into the settled record.

Without this, the ghost report can show `lane_min_edge` as a reason string, but none of the underlying config state (what min_edge was set to, whether it was reached) is recorded. The feedback loop cannot close.

**Fix:** Add to `REJECTED_COPY_FIELDS`:
```python
REJECTED_COPY_FIELDS = (
    # ... existing fields ...
    "lane_min_edge",      # add: the effective min_edge threshold at rejection time
    "effective_min_edge", # already in context but ensure it's propagated
)
```

Also in `settle_rejected_candidates()`, ensure that when copying from the rejected record to the settled record, `effective_min_edge` is included even when the rejection happened at a different stage. The `effective_min_edge` field is already computed by the strategy at rejection time — it just needs to survive the copy.

**Verification:** After fix, query settled ghost data for `reason: "lane_min_edge"` rows and confirm they now carry the actual `effective_min_edge` value. Example:
```bash
grep "lane_min_edge" data/calibration/rejected_candidates_settled.jsonl | python3 -c "import sys,json; rows=[json.loads(l) for l in sys.stdin]; print([r.get('effective_min_edge') for r in rows[:10]])"
```

---

## Fix 3: Add existence assertions to LaneEntryPolicy
**File:** `src/analysis/lane_entry_policy.py`

**Root cause:** `resolve_lane_entry_policy()` silently defaults all keys to 0.0 when config is missing:
```python
params: Dict[str, Any] = {
    "min_edge": 0.0,       # silently 0.0 when missing
    "hard_min_edge": 0.0,
    "ai_override_min_edge": 0.0,
    ...
}
```
AI editors see `min_edge: 0.0` and assume it's the configured value. After refactoring, if a config key was renamed or removed, the system silently falls back to 0.0 instead of crashing.

**Fix:** Add an assertion mode that raises on missing required keys:
```python
def resolve_lane_entry_policy(
    *,
    strategy_name: str,
    window_size: str,
    side: str,
    full_config: Dict[str, Any],
    legacy_policy: Optional[Dict[str, Any]] = None,
    _assert: bool = False,  # internal flag, default False for backward compat
) -> LaneEntryPolicy:
    """Resolve lane-specific entry policy for a strategy/window/side tuple.
    
    Raises ValueError if required keys are missing from config when _assert=True.
    """
    params: Dict[str, Any] = {
        "enabled": True,
        "min_edge": 0.0,
        "hard_min_edge": 0.0,
        "ai_override_min_edge": 0.0,
        "entry_price_min": 0.0,
        "entry_price_max": 1.0,
        "entry_window_min": 0.0,
        "entry_window_max": 0.0,
        "size_multiplier": 1.0,
    }
    
    # Validate required config sections exist before reading
    if _assert:
        strategies_cfg = full_config.get("strategies", {}) if isinstance(full_config, dict) else {}
        if strategy_name not in strategies_cfg:
            raise ValueError(
                f"Strategy '{strategy_name}' not found in config. "
                f"Available: {list(strategies_cfg.keys())}. "
                f"Refactoring gap: check if '{strategy_name}' was renamed or removed."
            )
        strat_cfg = strategies_cfg.get(strategy_name, {})
        entry_cfg = strat_cfg.get("entry_policy", {})
        if not entry_cfg:
            raise ValueError(
                f"entry_policy missing for strategy '{strategy_name}'. "
                f"Refactoring gap: entry_policy section was removed or renamed."
            )
    
    # ... rest of existing resolution logic unchanged ...
```

Call this with `_assert=True` in the strategy's scan path (not at construction — that's boot time and config may not be fully loaded). The assertion fires at scan time when the strategy is actually trying to use the policy.

**Verification:** After adding assertions, run the bot through a scan cycle and confirm it crashes loudly with a clear message if any config is missing, rather than silently using 0.0.

---

## Fix 4: Wire per_lane_thresholds_enabled or remove the feature
**File:** `src/analysis/lane_calibration.py` + `config/settings.yaml`

**Root cause:** `lane_thresholds.json` exists with computed `veto_recommended: true` entries, but `per_lane_thresholds_enabled` defaults to `False` in `LaneCalibrator.__init__()`. The feature is fully built but never activated.

This means:
- The ghost data says certain lanes should be vetoed
- The code to apply those vetos exists
- The config flag to enable it is off by default

AI editors see the feature, assume it's working, and recommend changes based on threshold data that's not actually being used.

**Fix (choose one):**

**Option A — Activate it:**
```yaml
# In config/settings.yaml under the lane_calibration section:
per_lane_thresholds_enabled: true
```
Then monitor for 48 hours and check if any lanes that should be open are getting vetoed.

**Option B — Remove it:**
If it's not trustworthy, remove the entire per_lane_thresholds feature (the `per_lane_thresholds_enabled` flag, the `_is_vetoed_for_lane()` logic that reads overrides, and the `compute_lane_thresholds()` / `write_lane_thresholds()` pipeline). Keep `lane_thresholds.json` as a report-only artifact.

**Recommendation:** Option A first — run with it enabled for 48 hours and compare the ghost report before/after. If lanes get incorrectly vetoed, you have data to calibrate the thresholds. If nothing changes, the feature isn't working as expected and Option B is the right call.

---

## Fix 5: Fix DOGE oracle or filter DOGE from scanner
**File:** `src/strategies/doge_macro.py` (or `src/market/scanner.py`)

**Root cause (from memory):** DOGE oracle returning `edge=0.0` on all `oracle_basis_block` checks. The oracle is not producing usable edge data, but DOGE signals still generate because edge=0.0 passes through to the rejection pipeline — where DOGE then hits the oracle_basis_block gate and gets rejected.

The result: 7 DOGE signals in the ghost log, 0 DOGE trades. The oracle is generating noise, not signal.

**Fix:** Add an oracle health check in `DOGEMacroStrategy` or the base scanner:
```python
# In the signal generation path, before emitting a DOGE candidate:
if self._oracle is not None:
    oracle_edge = getattr(self._oracle, 'last_edge', None)
    if oracle_edge is None or oracle_edge == 0.0:
        logger.warning(
            "DOGE oracle unhealthy: edge=%s. Skipping DOGE signals until oracle recovers.",
            oracle_edge
        )
        return []  # or: skip DOGE markets this cycle
```

Alternatively, add a `doge_oracle_enabled: bool` config flag that defaults to `False` until the oracle is fixed.

**Verification:** After fix, DOGE should either produce trades (if oracle is fixed) or produce zero signals (if oracle is filtered). Confirm ghost log has zero DOGE entries while oracle is broken.

---

## Summary Table

| # | Fix | Files | Type | Priority |
|---|-----|-------|------|----------|
| 1 | Filter null realized_pct from ghost aggregations | `ghost_calibration.py`, `ghost_gate_report.py` | Bug fix | P1 |
| 2 | Add lane_min_edge to REJECTED_COPY_FIELDS | `ghost_calibration.py` | Data quality | P1 |
| 3 | Existence assertions on LaneEntryPolicy config keys | `lane_entry_policy.py` | AI editor reliability | P2 |
| 4 | Wire or remove per_lane_thresholds feature | `lane_calibration.py`, `settings.yaml` | Feature decision | P2 |
| 5 | Fix or filter DOGE oracle | `doge_macro.py` or `scanner.py` | Data quality | P3 |

---

## AI Editor Instruction

When making any changes to PSB:
1. Check `REJECTED_COPY_FIELDS` in `ghost_calibration.py` before adding new rejection fields — new fields must be added there or they won't survive the ghost settlement pipeline
2. Check `lane_entry_policy.py` for any config key renames — missing keys silently become 0.0
3. After any refactoring, run `ghost_gate_report.py` and confirm row counts are stable
4. Never assume a config flag default of `False` means "intentionally off" — check the code to confirm whether it's a real default or a placeholder
