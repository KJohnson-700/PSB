## Probe-aware ghost gate audit — 2026-05-18

### Scope

- Data source: [data/calibration/rejected_candidates_settled.jsonl](/Users/mainfolder/Documents/psb-main%201/data/calibration/rejected_candidates_settled.jsonl)
- Tooling: [tools/ghost_gate_report.py](/Users/mainfolder/Documents/psb-main%201/tools/ghost_gate_report.py)
- Run date: 2026-05-18
- Purpose: establish an overnight baseline for settled ghost gates with **probe-backed** relaxation candidates, without changing live strategy behavior.

### Summary

- The settled ghost ledger has **68,179** rows.
- Overall ghost economics remain slightly protective in aggregate: `net_gate=+1835.931`, so the answer is not "loosen everything."
- The strongest **gate-level** missed-EV families are still BTC short histogram rejects:
  - `bitcoin|15m|BUY_NO|hist_gate_15m_short_reject` → `n=7668`, `WR=54.3%`, `CI_low=53.2%`, `netGate=-730.987`
  - `bitcoin|1h|BUY_NO|hist_gate_1h_short_reject` → `n=1967`, `WR=68.2%`, `CI_low=66.1%`, `netGate=-745.060`
- The strongest **probe-backed** relaxations now visible in settled data are:
  - `bitcoin|15m|BUY_NO|hist_gate_15m_short_reject|hist_support_count`
    - `recommended_action: relax hist_support_count by 1.000000`
    - `n=2821`, `WR=52.2%`, `CI_low=50.3%`, `netGate=-163.777`
  - `bitcoin|1h|BUY_NO|hist_gate_1h_short_reject|hist_support_count`
    - `recommended_action: relax hist_support_count by 1.000000`
    - `n=1082`, `WR=62.75%`, `CI_low=59.83%`, `netGate=-300.385`
  - `eth_macro|1h|BUY_NO|oracle_basis_block|oracle_basis_abs_bps`
    - `recommended_action: relax oracle_basis_abs_bps by 5.000000`
    - `n=294`, `WR=61.22%`, `CI_low=55.54%`, `netGate=-66.547`

### Interpretation

- This validates the architectural pushback on Hermes:
  - broadening `performance_feedback.overtight_reasons` alone is **not** enough
  - the current runtime loop is still `min_edge`-specific
  - probe-aware gate families need operator review or new runtime logic
- BTC 15m remains the biggest missed-EV gate by raw economics, and it now also qualifies for a conservative probe-backed relax recommendation on `hist_support_count`.
- ETH weak-confirm still does **not** show up as a probe-backed relax candidate here, which is consistent with earlier caution that it may still be protective.

### What changed in code

- [tools/ghost_gate_report.py](/Users/mainfolder/Documents/psb-main%201/tools/ghost_gate_report.py) now emits:
  - probe win/loss counts
  - probe Wilson confidence intervals
  - `actionable_probe_relaxations`
- No live config, threshold, or execution logic changed.

### Overnight use

- Safe to let the bot run overnight with current strategy behavior unchanged.
- Tomorrow morning, rerun:

```bash
python3 tools/ghost_gate_report.py --limit 20
```

- Compare:
  - whether BTC 15m and BTC 1h histogram probe candidates continue to hold with larger sample
  - whether ETH 1h oracle-basis remains negative-net with larger sample
  - whether any ETH weak-confirm family begins to appear as a relax candidate

### Metadata / Summary

- **Tags:** `#PSB` `#GhostTrades` `#GateAudit` `#ProbeVariants` `#BTC` `#ETH`
- **Related Concepts:** [[Ghost Gate Report]], [[Probe Variants]], [[Runtime Feedback]], [[Lane-Aware Gates]]
- **Summary:** The settled ghost report now distinguishes raw gate families from probe-backed threshold candidates. As of 2026-05-18, the cleanest probe-supported relax signals are BTC 15m histogram support, BTC 1h histogram support, and ETH 1h oracle basis, while ETH weak-confirm still does not qualify for loosening.
