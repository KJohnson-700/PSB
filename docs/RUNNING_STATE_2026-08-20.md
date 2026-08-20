# PSB RUNNING STATE — captured 2026-08-20

Authoritative map of what the LIVE process is actually executing. Written because the
distinction between "on disk" and "loaded in the running process" caused several wrong
conclusions in this session, and because the operator asked for the running state to be
committed cleanly **without any staged/unapplied work mixed in**.

---

## 1. THE LIVE PROCESS

| | |
|---|---|
| PID | **870** |
| started | 2026-08-19 23:25:18 |
| session dir | `data/paper_trades/test_20260819_232520` |
| mode | **PAPER** (`trading.dry_run: true`) — verified in the loaded config |
| config file mtime at start | 23:25:02 (so the process read the config as of commit `bbdc71e`) |
| RSS | 1388MB (guard 1800) |

### What is LOADED (committed at or before the 23:25:18 start)
| commit | what it does |
|---|---|
| `bbdc71e` | favorite floor **0.85 → 0.89** |
| `9d58a3d` | **paper maker simulation** (`paper_entry_maker_sim: true`, cross-on-miss) |
| `482969a` | live verification of telemetry + 15m stop |
| `7c937eb` | probation cutoff fix |
| `3288cbe` | **cross-asset directional exposure cap** (`max_directional_exposure: 0.45`) |
| `0e5b046` | `early_stop_windows: ["15m","1h"]` — 15m favorites finally protected |
| `c667e08` | **resolution-path exit telemetry** (mae/mfe/pnl on `RESOLVED:*` exits) |

### What is NOT loaded (committed AFTER the process started)
| commit | status |
|---|---|
| `7ca2b8a` — per-token cache eviction (the RSS ratchet fix) | **on disk only.** Needs a restart. RSS 1388MB is still the un-evicted behaviour. |

### What is NOT applied at all (staged, deliberately excluded from this commit)
- `config/settings.yaml.STAGED_bundle_halts_stopwindows`
- `config/settings.yaml.STAGED_fav_stop_all_windows`
- `config/settings.yaml.STAGED_hybrid_engines_on`

These are superseded snapshots kept for audit. **They are NOT the live config** and are
git-ignored so they can never be mistaken for it.

---

## 2. LIVE CONFIG — the settings that define current behaviour

### Favorite lane (the primary book)
```yaml
favorite_lane:
  enabled: true
  floor: 0.89            # raised from 0.85 — WR 82.1% vs 77.4% (n=290 vs 531)
  price_max: 0.93
  size_usd: 70.0
  windows: ["15m", "1h"]
  early_stop_windows: ["15m", "1h"]   # BOTH protected (was code-default ["1h"])
  hard_stop_price: 0.70
  min_mins_left: 3.0
  presettle_derisk_secs: 180
  presettle_derisk_price: 0.55
  respect_ai_direction: false
risk:
  favorite_lane_enabled: true          # the SECOND switch, checked FIRST
```
⚠️ `risk.favorite_lane_enabled` is evaluated before `favorite_lane.enabled` and returns an
empty signal list on its own. Toggling only the latter does nothing. This is what silently
killed favorites on ~08-10.

### Risk / exposure
```yaml
trading:
  max_directional_exposure: 0.45   # NEW: caps same-side notional ACROSS ALL ASSETS
  max_topic_exposure: 0.20         # per asset|direction (blind to correlation on its own)
  max_position_size: 80
  cycle_interval_sec: 30
  exit_check_interval_sec: 3       # fast-exit monitor
risk:
  portfolio_halts: {enabled: true, session_loss_halt_pct: 6.0, hard_halt_pct: 12.0}
```

### Execution
```yaml
trading:
  entry_mode: hybrid
  entry_maker_wait_sec: 8
  paper_entry_maker_sim: true        # NEW — paper previously forced 100% taker fills
  paper_maker_cross_on_miss: true    # preserve frequency while measuring the true rate
exit_rules:
  updown_flatten_before_resolution_sec: 210
  hold_all: true
```

### Band engine
```yaml
direction:
  side_policy: favorite
  side_policy_price_band: [0.45, 0.55]
  side_policy_flat_edge: 0.02        # floors admission edge, bypassing min_edge
```

---

## 3. LIVE SESSION SO FAR (`test_20260819_232520`)

**13 entries (5 at 0.89+) · 10 closes · WR 70% · realized +$42.83 · avgW +$8.01 / avgL −$4.42**

Exit mix: `RESOLVED:YES (real)` 4 · `hold_catastrophic_stop` 3 · `updown_expired_mark_fallback` 3
Entry prices: 0.19, 0.51×3, 0.53, 0.54, 0.55, 0.80, 0.90, 0.91×2, 0.92, 0.93 — all BUY_YES.

**avgL is −$4.42**, versus −$23.06 and −$31.39 in the two prior sessions. That is the largest
single improvement of the session and it is what the loss-containment work was for.

Maker simulation: **3 fills / 13 attempts (23%)** — the first maker fills in this bot's history.

---

## 4. PROBATION — graded from OUTPUT, not flags
| row | state |
|---|---|
| `resolution_exit_telemetry` | ✅ PROVEN 3/3 |
| `paper_maker_sim` | ✅ PROVEN (fill rate measured) |
| `rss_no_ratchet` | ✅ PROVEN (but the fix itself is not loaded — see §1) |
| `portfolio_halt_ladder` | 🟡 PROBATION, wired |
| `bot_alive` | ✅ PROVEN |

---

## 5. KNOWN-OPEN, NOT ACTED ON

1. **`min_mins_left: 3.0` is cutting the best zone.** Favorites by minutes-left at entry:
   3–5min WR 83% / −$0.27 · 5–10min 80% / −$0.79 · 10+min 71% / −$1.47 (n=488, monotonic).
   Under 3 minutes we have **2 trades** because the gate forbids it. Proposed change is
   `min_mins_left: 1.5` **paired with** `presettle_derisk_secs: 90` (the two were designed as a
   matched pair at 180s = 3min; moving one alone invites a subtle bug).
   **Operator decision: let the current session run longer first.**
2. **15m stop fills are ~half as good as 1h** (median 0.645 vs 0.688 against a 0.70 trigger;
   live worst 0.495). The exit monitor already runs every 3s, so this is market gapping, not
   polling. Remaining fix is execution: resting maker order at the stop, or buy-opposite.
3. **Near-resolution zone unmeasured.** `price_max: 0.93` + `min_mins_left: 3.0` keep us out of
   the 0.95–0.99 / final-60s region entirely (13 lifetime trades above 0.95). Probe running:
   `scripts/psb_near_resolution_probe.py` + `_grade.py`, sampling T-60s and T-45s.
4. **170 per-lane time keys** exist (`entry_window_min`/`max` per asset × window × side) and
   none were set from measurement. Not audited.

---

## 6. REVERTS, all pre-written
| change | revert |
|---|---|
| favorite floor 0.89 | `floor: 0.85` |
| maker sim | `paper_entry_maker_sim: false` |
| directional cap | `max_directional_exposure: 0` (0/missing = OFF) |
| early_stop_windows | `["1h"]` |
| halts | remove `risk.portfolio_halts` |
| cache eviction | revert `7ca2b8a` |

Config backups from tonight: `settings.yaml.bak_pre_floor089_232502`,
`bak_pre_makersim_231333`, `bak_pre_bundle_*`, `bak_pre_engines_on_190231`.
