# Dead Zone Toggle — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add `dead_zone_enabled: true/false` toggle per strategy in settings.yaml. When false, the dead zone block is bypassed and all eligible markets pass through. When true (default=existing behavior), blocks as normal.

**Why:** Dead zones were created from early-session data that had bugs (Hermes weather changes, bad liquidity, scanner timeouts). Need to toggle them off to collect clean A/B data before deciding which hours are actually bad.

**Architecture:** One new config flag per strategy. Gate the dead zone block in both bitcoin.py and sol_macro.py with an early-exit when the flag is false. Record every market that would have been blocked in the journal so we can backtest the outcomes.

---

## Context for Implementer

**Key files to touch:**
- `config/settings.yaml` — add `dead_zone_enabled` flag per strategy section
- `src/strategies/bitcoin.py` line ~761 — gate the dead zone block
- `src/strategies/sol_macro.py` line ~847 — gate the dead zone block
- `src/paper/journal.py` or wherever trades get recorded — add dead_zone_skip journal event

**Strategy sections in settings.yaml:**
- `strategies.bitcoin` — line ~106
- `strategies.sol_macro` — line ~174
- `strategies.eth_macro` — line ~245
- `strategies.hype_macro` — line ~309
- `strategies.xrp_macro` — line ~347

**Dead zone block locations:**
- BTC: `src/strategies/bitcoin.py` line 761 — `if _now_utc_hour in _blocked_hours:`
- SOL/HYPE/XRP/ETH: `src/strategies/sol_macro.py` line 847 — `if _now_utc_hour in _blocked_hours:`

**Config key pattern:** `self.config.get("dead_zone_enabled", True)` — default True so existing behavior is preserved.

---

## TASK 1: Add `dead_zone_enabled: true` to settings.yaml for all 5 strategy sections

**Files:**
- Modify: `config/settings.yaml` (add flag to each strategy section)

**Step 1: Find the line ranges for each strategy section**

In settings.yaml, find each strategy's section header and add `dead_zone_enabled: true` under it.

Strategy sections (grep for `strategies.bitcoin:`, `strategies.sol_macro:`, etc.):

```
strategies.bitcoin:       # line ~106
strategies.sol_macro:     # line ~174
strategies.eth_macro:     # line ~245
strategies.hype_macro:    # line ~309
strategies.xrp_macro:     # line ~347
```

Each section starts with `strategies.<name>:` and contains config keys like `enabled:`, `min_edge:`, etc.

**Step 2: Add the flag to each section**

Under each strategy section header, add:

```yaml
  dead_zone_enabled: true   # When false, skip dead-zone UTC hour filter — collect A/B data
```

Place it near the top of each section, after `enabled:` or near `blocked_utc_hours_updown`.

**Step 3: Verify the changes**

```bash
grep -n "dead_zone_enabled" config/settings.yaml
```

Expected output: 5 lines, one per strategy section, all with value `true`.

**Step 4: Commit**

```bash
git add config/settings.yaml
git commit -m "feat: add dead_zone_enabled flag to all strategy sections"
```

---

## TASK 2: Gate the dead zone block in bitcoin.py

**Files:**
- Modify: `src/strategies/bitcoin.py` lines ~755-770

**Step 1: Read the current dead zone block**

```python
# Current code at ~761:
_blocked_hours = self.config.get("blocked_utc_hours_updown", [])
if _now_utc_hour in _blocked_hours:
    _bump_skip("blocked_utc_hour")
    logger.info(
        f"  BTC skip updown at UTC {_now_utc_hour:02d}:xx — "
        f"dead-zone hour ({_now_utc_hour}:00 UTC <35% WR in live data)"
    )
    continue
```

**Step 2: Wrap with dead_zone_enabled check**

Replace the above with:

```python
# ── UTC hour filter (dead zone) ──
_dead_zone_enabled = self.config.get("dead_zone_enabled", True)
if _dead_zone_enabled:
    _blocked_hours = self.config.get("blocked_utc_hours_updown", [])
    if _now_utc_hour in _blocked_hours:
        _bump_skip("blocked_utc_hour")
        logger.info(
            f"  BTC skip updown at UTC {_now_utc_hour:02d}:xx — "
            f"dead-zone hour ({_now_utc_hour}:00 UTC <35% WR in live data)"
        )
        continue
else:
    logger.debug(
        f"  BTC dead_zone DISABLED — allowing UTC hour {_now_utc_hour:02d} "
        f"(blocked_hours={_blocked_hours})"
    )
```

**Step 3: Verify the change**

```bash
grep -n "dead_zone_enabled\|blocked_utc_hour" src/strategies/bitcoin.py | head -10
```

Should show `_dead_zone_enabled` near the dead zone block.

**Step 4: Commit**

```bash
git add src/strategies/bitcoin.py
git commit -m "feat: gate BTC dead zone with dead_zone_enabled toggle"
```

---

## TASK 3: Gate the dead zone block in sol_macro.py

**Files:**
- Modify: `src/strategies/sol_macro.py` lines ~842-855

**Step 1: Read the current dead zone block**

```python
# Current code at ~847:
_blocked_hours = self.config.get("blocked_utc_hours_updown", [0, 18, 22])
_now_utc_hour = datetime.now(timezone.utc).hour
if _now_utc_hour in _blocked_hours:
    logger.info(
        f"  {_alt_label} skip updown at UTC hour {_now_utc_hour}:xx — "
        f"blocked dead zone (config: {_blocked_hours})"
    )
    continue
```

**Step 2: Wrap with dead_zone_enabled check**

Replace the above with:

```python
# ── Dead zone (UTC hour filter) ──
_dead_zone_enabled = self.config.get("dead_zone_enabled", True)
if _dead_zone_enabled:
    _blocked_hours = self.config.get("blocked_utc_hours_updown", [0, 18, 22])
    _now_utc_hour = datetime.now(timezone.utc).hour
    if _now_utc_hour in _blocked_hours:
        logger.info(
            f"  {_alt_label} skip updown at UTC hour {_now_utc_hour}:xx — "
            f"blocked dead zone (config: {_blocked_hours})"
        )
        continue
else:
    _blocked_hours = self.config.get("blocked_utc_hours_updown", [0, 18, 22])
    _now_utc_hour = datetime.now(timezone.utc).hour
    logger.debug(
        f"  {_alt_label} dead_zone DISABLED — allowing UTC hour {_now_utc_hour:02d} "
        f"(would-be blocked_hours={_blocked_hours})"
    )
```

**Step 3: Verify the change**

```bash
grep -n "dead_zone_enabled\|blocked.*dead_zone\|skip updown" src/strategies/sol_macro.py | head -15
```

**Step 4: Commit**

```bash
git add src/strategies/sol_macro.py
git commit -m "feat: gate SOL/HYPE/XRP/ETH dead zone with dead_zone_enabled toggle"
```

---

## TASK 4: Add dead zone skip events to the journal for backtesting

**Objective:** When dead_zone_enabled=false and the hour would have been blocked, record it in the journal so we can later look up the market outcome.

**Files:**
- Modify: `src/paper/journal.py` (or wherever signal/trade events are logged — find it with grep)

**Step 1: Find the journal event structure**

```bash
grep -rn "journal\|Journal\|signal.*event\|trade.*event" src/paper/ | head -20
grep -rn "write.*journal\|journal.*write\|record.*trade" src/ | head -10
```

Look for where the strategy signals get recorded. The pattern will be something like:
- `journal.record_signal(...)` or
- `journal.write_event(...)` or
- A dict/event logged to a JSON file

**Step 2: Add a dead_zone_skip event**

When dead_zone_enabled=false and a market would have been blocked (i.e., the code reaches the else branch), emit a journal event:

```python
{
    "event": "dead_zone_skip",
    "timestamp": "<ISO timestamp>",
    "strategy": "<strategy_name>",
    "market_id": "<market.id>",
    "question": "<market.question>",
    "would_be_blocked_hour": _now_utc_hour,
    "blocked_hours_config": _blocked_hours,
    "resolved": null,       # filled in when market resolves
    "outcome": null,        # filled in when market resolves: "yes_won" / "no_won"
    "edge": <edge_value>,
    "signal_direction": "<LONG/SHORT>",
}
```

**Step 3: Find where market resolution is recorded**

After a market resolves, the journal should record the outcome. Add logic there to check if there's a matching `dead_zone_skip` event and fill in `resolved`, `outcome`.

```bash
grep -rn "market.*resolv\|resolution\|outcome\|yes_price.*end" src/paper/ | head -20
```

**Step 4: Verify and commit**

Run the bot paper mode for a few cycles and check the journal for `dead_zone_skip` entries.

```bash
git add src/paper/journal.py  # or wherever you modified
git commit -m "feat: record dead_zone_skip events for A/B analysis"
```

---

## TASK 5: Test — toggle dead zone off for SOL and run paper mode

**Objective:** Verify the toggle works end-to-end.

**Step 1: Set dead_zone_enabled: false for sol_macro only in settings.yaml**

```yaml
strategies:
  sol_macro:
    dead_zone_enabled: false
    # other config...
```

**Step 2: Start paper bot**

```bash
cd "/Users/mainfolder/Documents/psb-main 1"
python -m paper.run  # or whatever the start command is
```

**Step 3: Verify the logs show "dead_zone DISABLED" for SOL**

```bash
grep "dead_zone DISABLED" data/logs/polybot_*.log
```

**Step 4: Verify SOL markets are now entering (not getting blocked by dead zone)**

```bash
grep "SOL Macro strategy:.*signal\|SOL diagnostics" data/logs/polybot_*.log
```

**Step 5: Re-enable after test**

Change `dead_zone_enabled: true` back for sol_macro.

---

## Verification Checklist

After all tasks:

- [ ] `dead_zone_enabled: true` present in all 5 strategy sections in settings.yaml
- [ ] BTC dead zone block gated with `_dead_zone_enabled` check in bitcoin.py
- [ ] SOL/HYPE/XRP/ETH dead zone block gated with `_dead_zone_enabled` check in sol_macro.py
- [ ] Bot starts without errors
- [ ] Logs show "dead-zone hour" messages when flag is true (existing behavior)
- [ ] Logs show "dead_zone DISABLED" messages when flag is false
- [ ] Journal records dead_zone_skip events when flag is false
- [ ] 4 out of 5 strategies can have dead zones off while 1 is on (for A/B comparison)

---

## Notes

- The `dead_zone_enabled` flag defaults to `True` in code — if the key is missing from settings.yaml, existing behavior is preserved.
- Set different strategies to different values for A/B comparison (e.g., BTC dead zone on, SOL dead zone off).
- Journal `dead_zone_skip` events let you backtest: for each skipped market, look up the actual outcome and compute WR for that hour.
- When dead zone is off, the rest of the strategy pipeline runs normally — edge filters, entry windows, AI vetoes still apply.
