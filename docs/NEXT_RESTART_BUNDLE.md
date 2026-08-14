# NEXT-RESTART BUNDLE — restart-class changes staged, apply on the next bot restart

**Rule:** a bot restart loads the ENTIRE working tree (committed + ALL uncommitted). Before any
restart: (1) `git diff --stat HEAD` to see the full runtime surface, (2) confirm `--paper`,
(3) SIGTERM the pid (never the dashboard shutdown endpoint), (4) verify behavior-vs-baseline after.
Restart is OPERATOR-GATED — never restart without explicit GO.

Legend: RESTART-CLASS = frozen at __init__ / not in the hot-reload set, so a config/code edit does
NOT take effect until a full restart. HOT = `self.config.get()` per-call, applies on config reload.

---

## STAGED (activate on next restart)

| # | change | file | class | backup | why | verify after restart |
|---|--------|------|-------|--------|-----|----------------------|
| 1 | `reversal_halt.enabled: true → false` | config/settings.yaml:2868 | RESTART-CLASS (frozen at `CircuitBreakerManager.__init__`) | settings.yaml.bak_pre_reversalhalt_kill_* | Codex-flagged + verified ACTIVE BTC_DECIDES_ALT: same-side concentration breaker keyed on BTC's return, gated alt entries; redundant with exposure.max_same_side_positions; demonstrably useless 08-03 (alts bounced up while BTC fell → stayed silent) | grep no `reversal_halt:` / `CIRCUIT_BREAKER … reversal_halt` fires in logs; confirm alt same-side entries no longer BTC-gated |

## NOTE — already LIVE (loaded by the 23:13 08-02 restart, NOT pending)
#110 BTC 3rd-vote (tape_map aux_dir), xrp15m+eth5m side-veto SHADOW, SOL→execution_strategies,
tape_map.py aux_dir/log_side_veto_shadow. These are running; do not re-stage.

## CANDIDATES (not yet staged — need build + operator GO before adding here)
- Freq-killer relaxations (Codex audit): min_edge 0.09, entry_price band 0.42–0.58, BNB rsi_hard_gate>53,
  enforce_alt_1h_alignment→soft, require_quant_side_agreement→shadow. Check HOT vs RESTART-CLASS per key.
- SOL 5m BUY_NO → defer to realized adapter (the refuted-tape-gate replacement).
- Vestigial BTC-residue cleanup (block_counter_macro_leg_updown, require_btc_catalyst_*, etc. — inert, cosmetic).
