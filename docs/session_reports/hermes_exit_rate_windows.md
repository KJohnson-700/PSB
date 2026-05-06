# Reproducible windows: Hermes “exit rate” comparison

This note fixes **exact** session lists and filters so “old vs new” comparisons can be reproduced from `data/paper_trades/*/entries.jsonl`. It supersedes informal labels like “new session” that did not map to a single `entries.jsonl` (e.g. `test_20260504_150648` alone has only **8** closed trades in-repo as of the last parse).

## Window A — “Old” baseline (single journal)

| Field | Value |
|--------|--------|
| **Session directory** | `test_20260504_034719` |
| **Exit-time filter** | None (all EXIT rows in that file after ENTRY/EXIT join) |
| **Session mtime filter** | None |

**Reproduce:**

```bash
python3 scripts/attribution_since.py \
  --label hermes_old_034719 \
  --sessions test_20260504_034719
```

**Artifacts:** [`attribution_since_hermes_old_034719.md`](attribution_since_hermes_old_034719.md), [`attribution_since_hermes_old_034719.json`](attribution_since_hermes_old_034719.json)

**Exit buckets (Hermes-aligned, from stratification block):** `take_profit` **75**, `updown_time_stop` **35**, `RESOLVED:YES (real)` **3**, `RESOLVED:NO (real)` **2**, `updown_expired` **0**, **115** closes. **tp_share** = TP ÷ (sum of those five buckets) ≈ **0.652** (75/115).

## Window B — “New” post-restart era (multi-session, exit-time floor)

| Field | Value |
|--------|--------|
| **Session dirs** | All `data/paper_trades/*` with directory **mtime ≥ 2026-05-04** (UTC midnight). *In-repo this is 7 sessions:* `test_20260503_164708`, `test_20260503_194556`, `test_20260503_223335`, `test_20260504_004812`, `test_20260504_025753`, `test_20260504_034719`, `test_20260504_150648`. |
| **Exit-time filter** | EXIT `timestamp` **≥** first JSONL line timestamp of `test_20260504_150648/entries.jsonl` (post-restart clock). |
| **Rationale** | Aligns with the attribution playbook: “current era” after the `150648` restart while still allowing **multi-session** merges from the May‑4 mtime cohort. |

**Reproduce:**

```bash
python3 scripts/attribution_since.py \
  --label hermes_new_post150648_mt \
  --from-first-line data/paper_trades/test_20260504_150648/entries.jsonl \
  --after-mtime 2026-05-04
```

**Artifacts:** [`attribution_since_hermes_new_post150648_mt.md`](attribution_since_hermes_new_post150648_mt.md), [`attribution_since_hermes_new_post150648_mt.json`](attribution_since_hermes_new_post150648_mt.json)

**Exit buckets (same definition):** `take_profit` **17**, `updown_time_stop` **17**, `RESOLVED:YES (real)` **2**, `RESOLVED:NO (real)` **2**, `updown_expired` **1**, **39** closes. **tp_share** ≈ **0.436** (17/39).

**Note on informal “36-trade” tables:** A slice with **17 + 14 + 2 + 2 + 1 = 36** closes differs by **3** trades from this reproducible window (here, three additional `updown_time_stop` exits). Any narrative should cite **either** this command **or** an explicit session list that reproduces their denominator.

## Stratified TP share (same windows)

Stratification is emitted automatically in each attribution report under **“Exit stratification (Hermes buckets)”**:

- **Overall** line: counts + `tp_share_of_hermes_buckets`.
- **By strategy:** TP vs `updown_time_stop` vs resolution vs expired, per strategy.
- **By strategy × `window_size`:** same for `5m` / `15m` / `unknown` (from ENTRY `extra.window_size`).

### Highlights (read from linked MD files)

| Window | eth_macro TP | eth_macro tp_share | hype_macro TP | hype_macro tp_share | bitcoin tp_share (all buckets) |
|--------|-------------:|-------------------:|--------------:|--------------------:|-------------------------------:|
| A `034719` | 7 | 0.78 | 12 | 0.80 | 0.54 |
| B post‑150648 ∧ mtime | 4 | 0.36 | 1 | 0.14 | 0.80 |

**Interpretation guardrail:** Lower overall TP share in Window B coexists with **large drops in eth_macro and hype_macro** stratified `tp_share`, not a uniform “market only” story across strategies—compare cells before attributing the whole session to a single regime draw.

## `tp_share` definition

\[
\text{tp\_share} = \frac{N(\text{take\_profit})}{N(\text{take\_profit}) + N(\text{updown\_time\_stop}) + N(\text{RESOLVED:Y}) + N(\text{RESOLVED:N}) + N(\text{updown\_expired})}
\]

Counts exclude `other` exit reasons from the denominator (numerator only counts `take_profit`). Implementation: `exit_stratification` in [`scripts/attribution_since.py`](../../scripts/attribution_since.py).
