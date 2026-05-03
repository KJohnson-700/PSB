# Live Polymarket last-mile checklist

Rank this **above** pure signal tuning before scaling live size.

## Order path

- [ ] **Fill confirmation:** async wait matches **actual** executed size and average price (CLOB response vs intent).
- [ ] **Partial fills / rejects:** logged and reconciled with risk/journal.

## Resolution

- [ ] **Resolution source:** confirm whether settlement uses **Gamma**, **on-chain resolution**, or another path — documented per flow.
- [ ] **Resolved flag trust:** validate against **market outcome** your position needs.

## Timing races

- [ ] **5m / 15m expiry during scan:** behavior defined — abort new entry if `mins_left` \< buffer; no stale submissions after window close.
- [ ] **Position aging:** open positions near expiry still receive exit logic (`exit_manager`, up/down rules).

## Paper vs live

- Paper may not charge **bid–ask** or **settlement lag** the same way — see `docs/PSB_PAPER_VS_LIVE_FRICTION.md`.
