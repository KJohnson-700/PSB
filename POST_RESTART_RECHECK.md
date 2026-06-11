# Post-Restart Recheck List

Items that can only be judged on **post-restart (post-epoch) data**, because the
fix that changes them is uncommitted/needs-restart. After each bot restart, work
through the open items below and tick or remove them once verified. Always
time-filter to **after the restart timestamp** (the pre-restart rows are stale).

---

## Open

### sol_macro 5m BUY_YES (LONG) — exit recheck (added 2026-06-10)

**Why it's here:** The exit-reason split (ts≥06-02, n=55) did **not** support
widening the stop. The 32 `updown_stop_loss` trades recover ~$80 if held, but are
still net −$62 at only **34% held WR** → mostly wrong-side, not cut-too-early.
Widening the stop there just bleeds losers slower (the bnb-5m wrong-lever trap).
The cleaner (but small, n=20) signal was the `take_profit` cohort capping winners
(+$64 left on the table, 95% held WR).

**What the restart changes:** the window-delta flip (now live post-restart) should
redirect the wrong-side longs to shorts, **shrinking the stop cohort at the
source**. So re-judge exits only on the new cohort.

**Recheck steps:**
1. Re-run the exit_reason split for `sol_macro 5m BUY_YES`, filtered to
   `ts >= <restart epoch>` (not 06-02).
2. If the `updown_stop_loss` cohort now shows **held WR ≥ ~50%** (genuinely
   cut-early winners) → widen the stop. If it's still <45% held WR → leave the
   stop (it's an entry/side problem the flip should be handling).
3. Separately, if `take_profit` still leaves material held-pnl on winners at high
   held WR with n≥~30 → loosen/trail the TP. Forward-test only.

---

### BTC hist_gate 15m/5m LONG soft-mode (added 2026-06-10)

**Change:** set `hist_gate_15m_long_hard_reject: false` + `hist_gate_5m_long_hard_reject: false`
(config/settings.yaml bitcoin) — matches the already-live 1h fix. Converts the
LONG histogram guillotine to a mild est_prob penalty (bitcoin.py:2222).

**Why:** `scripts/gate_noise_audit.py` (ranks gates by EV of what they BLOCK)
showed these hard-rejected +EV longs: ghost blocked-pool EV +0.074 (5m, still
firing 5.7h ago), +0.023 (15m, n=4401, dormant only because BTC 4H is rising —
will bite when 4H decelerates). lane_min_edge by contrast is ~EV-neutral (−0.013)
— NOT the villain; `lane_min_edge_bias_quant_disagree` (−0.052) and
`bull_regime_expensive_short` (−0.181) are genuinely protective, keep them.

**Recheck steps:**
1. After restart, confirm `hist_gate_15m_long_reject` / `hist_gate_5m_long_reject`
   stop appearing as HARD rejects (should show `hist_gate_<w>_long_penalty` in
   reason_parts instead) and BTC long frequency rises.
2. Re-run `python scripts/gate_noise_audit.py --strategy bitcoin --since <restart>`:
   the softened longs should move out of the blocked pool; verify the admitted
   longs realize ~the +EV the ghost predicted (watch the weak cheap-band tail).

## Also verify this restart (window-delta work, 2026-06-10)

- [ ] **Shadow logger is writing.** Confirm `data/calibration/window_delta_shadow.jsonl`
      is being appended (up/down candidates). Then after ~1 day:
      `python scripts/window_delta_shadow_settle.py --window 5m`
      → if "tape beats market price" buckets are +EV, promote window-delta to the
      primary 5m/15m trigger. The +40% TP stays decoupled (test separately).
- [ ] **eth 15m LONG + doge 5m LONG flips are firing.** These bled pre-restart
      (eth held 41%/real 21%/−$52; doge held 45%/real 27%/−$133) but the flip was
      already wired+enabled — the bleed was stale. Confirm `window_delta_flip->`
      stamps appear and the realized side-mix shifts. Do **not** add new flip code.
- [ ] **Starved high-EV 1h lanes** (ghost, n large): bnb 1h LONG (+0.200),
      doge 1h LONG (+0.138), hype 1h SHORT (+0.124) — confirm they start trading;
      if still ~0 live, find the blocker (admission gate / floor-bump), don't tighten.
