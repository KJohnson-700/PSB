# Timestamps and timezones (PSB)

## Canonical ops clock

- **`OPS_JSON`** and **`/api/ops/summary`** field **`ts`** are **UTC**, ISO 8601 (`datetime.now(timezone.utc).isoformat()` in `src/ops_pulse.py`).
- **`timestamps_policy.canonical`** in the same payload is **`UTC`**.

## Known mixed sources

- **Journal / paper files:** may use local or mixed conventions depending on writer.
- **Log lines:** human-readable `datetime` without explicit offset in some paths.
- **Dashboard “today” / daily rollups:** server may use **UTC** calendar day (see AGENTS.md) while operator intent is **America/Los_Angeles** for “yesterday’s session.”

## Forensic workflow

1. Anchor on **`ops_ts` (UTC)** from `/api/ops/summary` when correlating to exchange candles (UTC-aligned).
2. For operator-local “calendar day,” slice using an explicit offset window (e.g. Pacific boundaries expressed in UTC).
