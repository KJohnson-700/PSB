# PSB Bot — Audit Fix Specification

**Date**: 2026-05-11  
**Auditor**: Hermes subagent  
**Path**: `/Users/mainfolder/Documents/psb-main 1/`  
**Output**: `SPEC.md` (plan only — no implementation)

---

## Summary of 5 Bugs Found

| # | Bug | File(s) | Priority |
|---|-----|---------|----------|
| 1 | **Duplicate EXIT events** — 230 EXITs for 23 trades (10× dupes) | `src/main.py`, `src/execution/trade_journal.py` | P0 |
| 2 | **`ai_used` always false** — decision_layer configured but bypassed | `src/strategies/bitcoin.py` | P1 |
| 3 | **Shadow pipeline `decision='?'` / `ts='?'`** — missing fields in `_run_shadow_pipeline` call | `src/analysis/ai_agent.py` | P1 |
| 4 | **`realized_pnl` wrong** — dupes sum INTO daily stats restore | `src/main.py` | P0 |
| 5 | **BTC 5m: 0% WR / 17 stop-loss hits** — tight cents stop fires in quiet markets | `bitcoin.py` / `config/settings.yaml` | P1 |

---

## Bug 1 — Duplicate EXIT Logging

### Root Cause
`_restore_daily_stats` (called on every mid-day bot startup) reads **all** EXIT lines from `entries.jsonl` without deduplicating by `trade_id`. Each mid-day restart re-sums the same EXIT records, inflating `daily_pnl` and `daily_trades` proportionally.

Secondary: `_load_state` in `trade_journal.py` also sums EXIT records per-session without a `_seen_trade_ids` dedup set.

### Affected Code
- **`src/main.py`** lines 612–641 (`_restore_daily_stats`): reads every EXIT, no trade_id dedup
- **`src/execution/trade_journal.py`** lines 1170–1204 (`_load_state`): `_MAX_PLAUSIBLE_PNL` guard only skips **phantom** exits (token flip or |pnl|>$200), not **duplicate** exits. No `_seen_trade_ids` set.

### Fix Location
`src/main.py` — `_restore_daily_stats`

### Exact File Changes

**File**: `src/main.py`  
**Context**: Lines 612–641 (`_restore_daily_stats`)  

**OLD** (no dedup):
```python
if entry.get("event") == "EXIT":
    pnl = entry.get("pnl", 0) or 0
    ...
    if abs(pnl) > max_plausible:
        ...
    else:
        daily_pnl  += pnl
        daily_trades += 1
```

**NEW** (dedup by trade_id):
```python
if entry.get("event") == "EXIT":
    pnl = entry.get("pnl", 0) or 0
    ...
    else:
        _tid = entry.get("trade_id", "")
        if _tid in _seen_exit_trade_ids:
            continue  # skip duplicate EXIT for same trade_id
        _seen_exit_trade_ids.add(_tid)
        daily_pnl  += pnl
        daily_trades += 1
```

**And** initialize `_seen_exit_trade_ids` before the loop:
```python
_seen_exit_trade_ids: set[str] = set()
```

**Second fix**: `src/execution/trade_journal.py` — `_load_state`  
Add a `_seen_exit_ids` set when iterating EXIT lines (right after line 1188):

```python
_exited_ids: set[str] = set()   # BEFORE the second pass loop
...
if e.get("event") == "EXIT":
    pnl = e.get("pnl", 0) or 0
    ...
    if is_token_flip or is_oversized:
        continue  # skip phantom (existing)
    _tid = e.get("trade_id") or ""
    if _tid in _exited_ids:   # NEW: skip duplicate per-session
        continue
    _exited_ids.add(_tid)
    exits_count += 1
    rpnl += pnl
    ...
```

---

## Bug 2 — `ai_used` Always False Despite `decision_layer` Configured

### Root Cause
`ai_used = False` is initialized at line 988 of `bitcoin.py`. The variable is set to `True` at three explicit points: line 1497 (marginal threshold AI), line 1602 (no-threshold AI-only), and line 1765 (updown AI assist block). **However**, the `_updown_oracle_5m_chain` path (lines 1128–1180 for 5m markets) calls `evaluate_trade_decision` internally and checks `ai_decision.approved` at line 1772, but **`ai_used` is never set to `True`** within that code path — only the local `ai_decision` variable is updated.

Additionally, when `evaluate_trade_decision` returns an `AIDecision` object with `shadow_result=None` (no shadow pipeline call made), the `ai_used` flag at the signal level remains `False` even though an AI decision was made and acted upon.

### Affected Code
- **`src/strategies/bitcoin.py`**: Lines 1128–1180 (`_updown_oracle_5m_chain` path — **missing `ai_used = True`**)

### Fix Location
`src/strategies/bitcoin.py` — `_updown_oracle_5m_chain` path (around line 1765, inside the `if ai_decision.approved` check that is actually still inside the non-5m path — the 5m path needs to be traced)

Actually, trace more carefully: `_updown_oracle_5m_chain` is called at lines 1128–1140. Inside it, `evaluate_trade_decision` is called and returns `ai_decision`. The result is checked at line 1772 (`if not ai_decision.approved: ai_decision_layer_skips += 1`). But inside that chain, `ai_used` is **never set to True** for the 5m path specifically.

### Exact File Change

**File**: `src/strategies/bitcoin.py`  
**Context**: Around line 1765 (after `ai_calls += 1`, `ai_used = True` for the updown marginal path — but the `_updown_oracle_5m_chain` path at 1128-1180 needs similar treatment)

**Look at**: `_updown_oracle_5m_chain` result handling — after `ai_decision = await self.ai_agent.evaluate_trade_decision(...)` returns, add:

```python
# Inside the _updown_oracle_5m_chain result check,
# after ai_decision is approved and we proceed to log/return:
ai_used = True   # NEW: flag AI usage for signal telemetry
```

**Specific location**: Inside `_scan_and_analyze_result` around line 1772 (`if not ai_decision.approved:` skip + bump) — confirm the 5m path at 1128-1180 also sets `ai_used = True`. If not, add at the equivalent point in the 5m path block.

**Also** check `signal.py` or wherever `BitcoinSignal` is dataclass'd — ensure `ai_used: bool` is passed through from strategy to `main.py`'s ENTRY logger (`extra["ai_used"]` at line 1661).

---

## Bug 3 — Shadow Pipeline Missing `ts` and `decision` ('?')

### Root Cause
In `ai_agent.py` lines 959–989, the shadow pipeline JSONL payload uses `ts_utc` from `datetime.utcnow()`, but the **updown marginal path** (the one calling `_run_shadow_pipeline`) may pass `None` for `quant_action`, `quant_edge`, and `quant_threshold` — these become `""` and `0.0` in the log, making `decision='?'` actually a `None`-to-string `'?'` conversion from the rendering layer.

The `ts='?'` likely means the `_append_jsonl` call failed or the `ts_utc` field was missing in one code path. Looking at `_append_jsonl` — it is defined but the grep returned no hits (meaning the method body was likely renamed or it's `_write_jsonl`).

Fields confirmed missing in shadow JSONL for BTC 5m path:
- `quant_action = ""` (passed as `None`)
- `quant_edge = 0.0` (passed as `None`)  
- `quant_threshold = 0.0` (passed as `None`)

### Affected Code
- **`src/analysis/ai_agent.py`**: Lines 959–989 (`run_shadow_pipeline` payload construction)
- **`src/strategies/bitcoin.py`**: Lines 1639–1650 (call site — `quant_action=None` is passed)

### Fix Location
`src/strategies/bitcoin.py` — call site around line 1639–1650 (where `run_shadow_pipeline` is called with `quant_action=None`)

Also fix `ai_agent.py` to guard `quant_action` etc. before building payload:

```python
quant_action_str = str(quant_action) if quant_action is not None else "BUY_YES"  # or lookup from signal
```

**Better**: Fix at `_run_shadow_pipeline` call sites (bitcoin.py lines 1647–1649) to always pass a non-None string for `quant_action` and valid floats for edge/threshold.

---

## Bug 4 — `realized_pnl` Wrong (Duplicates Summed)

### Root Cause
Same as Bug 1. `_restore_daily_stats` reads all EXIT lines without dedup by `trade_id`. On every mid-day restart, `_restore_daily_stats` is called **again** (from `__init__` → `_load_state` chain), re-summing already-counted EXIT records.

Secondary: `underperformance_audit.py` lines 84–130 (`load_closed_trades`) pairs ENTRY and EXIT by `trade_id`, but if duplicates exist, each paired trade has PnL counted N times.

### Affected Code
- **`src/main.py`** lines 612–641 (`_restore_daily_stats` — same fix as Bug 1)
- **`src/analysis/underperformance_audit.py`** lines 84–130 (`load_closed_trades`) — should deduplicate by `trade_id` before aggregating

### Fix Location
`src/main.py` — `_restore_daily_stats` (same as Bug 1)  
`src/analysis/underperformance_audit.py` — `load_closed_trades`

### Exact File Change

**File**: `src/analysis/underperformance_audit.py`  
**Context**: `load_closed_trades` function (lines 84–130)

Add dedup set inside the function:

```python
def load_closed_trades(...) -> List[ClosedTrade]:
    ...
    _seen_trade_ids: set[str] = set()
    for session_id in sessions:
        ...
        for line in fh:
            ...
            if payload.get("event") == "EXIT" and trade_id:
                if trade_id in _seen_trade_ids:
                    continue  # skip duplicate EXIT
                _seen_trade_ids.add(trade_id)
            rows.append(...)
```

Or alternatively, in `aggregate_trade_rows` (line 184) where `rpnl` and `exits_count` are accumulated, guard with `_seen_trade_ids`.

---

## Bug 5 — BTC 5m: 0% Win Rate / 17 Stop-Loss Hits

### Root Cause
BTC 5m markets are tight-range markets. With `updown_stop_cents=0.03` (3¢), positions in quiet BTC grinding (small ≤$30 moves) hit the stop-loss before momentum materializes. Combined with `look_ahead_5m=3` which may enter on late-cycle 5m candles already near resistance, positions enter and immediately reverse against entry.

Additionally, the `neutral_15m_min_composite_score=0.68` guard fires for 5m NEUTRAL paths, pushing entries into AI-only path where the strict stop (`updown_stop_cents=0.03`) may fire before resolution.

Config in `bitcoin.py` line 246: `blocked_utc_hours_updown: [0, 1, 2, 7, 11]` — but for 5m specifically, these are the same Tier-A guards for 15m, with **no 5m-specific hour blocking**.

### Affected Code
- **`config/settings.yaml`** lines 86–93: `updown_stop_cents=0.03` is too tight for BTC 5m
- **`src/strategies/bitcoin.py`** lines 1128–1180: `_updown_oracle_5m_chain` lacks 5m-specific hour blocking

### Fix Location
`config/settings.yaml` — `bitcoin.strategies.updown_overrides` (add `bitcoin:` overrides for 5m tightening):

```yaml
updown_overrides:
  bitcoin:
    updown_stop_cents: 0.05        # Widen from 0.03 — 5m needs room vs tight range
    updown_exit_window_mins: 3.0  # Widen from 2.25 — 5m look-ahead is longer
    updown_max_hold_mins: 20.0    # Safety: allow hold to near-natural resolution
```

Or: **recommend disabling BTC 5m** (`look_ahead_5m=0` or `min_edge_5m` clamped very high until live sample matures):

```yaml
bitcoin:
  look_ahead_5m: 0   # Disable 5m until live sample >= 15 trades with WR>0.46
```

**Alternative** (code-level): In `bitcoin.py` line 246 (`blocked_utc_hours_updown`), add 5m-specific hour blocks, or add `is_5m=True` branch inside `_updown_oracle_5m_chain` to skip markets when `mins_left < 1.0` (late entry already in stop window).

---

## Priority Order

| Priority | Bug | Fix Files | Lines |
|----------|-----|-----------|-------|
| **P0** | Bug 1 + Bug 4 (dupEXIT, wrong PnL) | `src/main.py`, `src/execution/trade_journal.py`, `src/analysis/underperformance_audit.py` | 612–641, 1188–1204, 84–130 |
| **P1** | Bug 2 (`ai_used=False`) | `src/strategies/bitcoin.py` | ~1765 |
| **P1** | Bug 3 (shadow missing data) | `src/strategies/bitcoin.py`, `src/analysis/ai_agent.py` | ~1647, 959–989 |
| **P1** | Bug 5 (BTC 5m stops) | `config/settings.yaml`, `bitcoin.py` | ~86–93, ~246 |

---

## Notes

- **Bug 1** and **Bug 4** share the same root cause (duplicate EXIT records, no dedup by trade_id). Fixing `_restore_daily_stats` in `main.py` addresses both simultaneously.
- **Bug 3** shadow fix requires confirming `_append_jsonl` method name (was renamed from `_append_jsonl` in grep — search for `_jsonl_append` or equivalent in `ai_agent.py`).
- **Bug 5** recommendation is to either widen `updown_stop_cents` for BTC 5m specifically, or set `look_ahead_5m=0` to disable the 5m path until live sample matures past the ≥15-trade threshold with WR>0.46.
- All changes are **plan only** — no implementation. Use this document to scope work items.