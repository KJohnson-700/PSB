# XRP Forensic Audit

## Scope
- Baseline session: `test_20260504_034719`
- Recent bad sessions: `test_20260430_010113, test_20260427_014233, test_20260504_220539`
- Generated at (UTC): `2026-05-06T05:20:12.015709+00:00`

## Performance Summary
- **Baseline**: 23 trades, net +2.38, WR 69.6%, wins/losses 16/7
- **Recent Bad**: 34 trades, net -49.32, WR 52.9%, wins/losses 18/16

## Exit-Path Contribution (Recent Bad Sessions)
| exit_reason | trades | net_pnl | WR | loss_share_of_negative_pnl |
|---|---:|---:|---:|---:|
| RESOLVED:YES (real) | 11 | -100.00 | 0.0% | 83.8% |
| updown_time_stop | 1 | -2.40 | 0.0% | 2.0% |
| RESOLVED:NO (real) | 10 | +21.41 | 60.0% | 14.2% |
| take_profit | 12 | +31.67 | 100.0% | 0.0% |

## BUY_NO Suppression Diagnostics
- Executed BUY_NO (baseline): `1`
- Executed BUY_NO (recent bad): `0`
- BUY_NO skip telemetry rows (xrp only): `0`
- Backtest BUY_NO net PnL: `+151.88`
- Backtest BUY_YES net PnL: `+131.93`

## Edge Quality (Recent Bad Sessions)
- `action:BUY_YES` -> n=9, net=-15.55, WR=44.4%, avg_edge=0.100
- `action:SELL_YES` -> n=25, net=-33.77, WR=56.0%, avg_edge=0.109
- `edge:0.08-0.10` -> n=15, net=-30.65, WR=40.0%, avg_edge=0.094
- `edge:0.10-0.12` -> n=12, net=-21.34, WR=58.3%, avg_edge=0.108
- `edge:>=0.12` -> n=7, net=+2.66, WR=71.4%, avg_edge=0.130
- `window:15m` -> n=16, net=-16.09, WR=62.5%, avg_edge=0.112
- `window:5m` -> n=18, net=-33.23, WR=44.4%, avg_edge=0.101

## Live-vs-Backtest Parity
```json
{
  "min_edge_15m_live_vs_backtest": {
    "live": 0.09,
    "backtest": 0.09
  },
  "min_edge_5m_live_vs_backtest": {
    "live": 0.07,
    "backtest": 0.07
  },
  "buy_no_extra_floor_live": {
    "live_min_edge_buy_no": 0.1,
    "note": "Backtest report does not encode live-only BUY_NO extra floor directly."
  },
  "entry_windows_live": {
    "entry_window_15m_min": 2.0,
    "entry_window_15m_max": 16.0,
    "entry_window_5m_min": 0.0,
    "entry_window_5m_max": 4.5
  },
  "regime_gate_live": {
    "enabled": true,
    "min_edge_mult": {
      "BULL": 1.0,
      "RANGE": 1.25,
      "BEAR": 1.4
    },
    "size_mult": {
      "BULL": 1.0,
      "RANGE": 0.7,
      "BEAR": 0.5
    }
  },
  "updown_exit_rule_live_global": {
    "updown_stop_cents": 0.03,
    "updown_exit_window_mins": 2.25,
    "updown_max_hold_mins": 18.0,
    "note": "Global updown exits apply to xrp_macro unless code adds strategy-specific overrides."
  },
  "backtest_control": {
    "report_file": "backtest_crypto_XRP_15m_20260505_034746.json",
    "trades": 1715,
    "net_pnl": 283.8,
    "buy_no_net_pnl": 151.875,
    "buy_yes_net_pnl": 131.925
  }
}
```

## Ranked Remediation Candidates
### A_buy_no_admission_relief (priority 1)
- Why now: Recent BUY_NO executions=0 vs baseline=1; backtest BUY_NO net=+151.88, BUY_YES net=+131.93.
- Proposed change: `{"xrp_macro.enforce_alt_1h_alignment": false, "xrp_macro.min_edge_buy_no": 0.08}`
- Predicted effect: Increase BUY_NO share and reduce one-sided BUY_YES drawdowns in adverse micro-regimes.
- Success metric: Within next 20 XRP closes: BUY_NO share >= 10% and XRP net PnL non-negative.
- Failure trigger: After 20 XRP closes, BUY_NO net PnL < -$3 or total XRP net PnL worsens vs pre-change.
- Rollback rule: Revert enforce_alt_1h_alignment=true and min_edge_buy_no=0.10.

### B_xrp_specific_time_stop_soften (priority 2)
- Why now: Recent updown_time_stop BUY_YES losses=2.40, 2.0% of recent negative PnL.
- Proposed change: `{"xrp_exit_rule_override": {"updown_stop_cents": 0.04, "updown_exit_window_mins": 1.5}}`
- Predicted effect: Reduce premature adverse exits and lower time-stop loss concentration.
- Success metric: updown_time_stop loss share of negative XRP PnL < 30% over next 20 XRP closes.
- Failure trigger: Two consecutive >$4 realized losses immediately after softening stop.
- Rollback rule: Restore current global updown_stop_cents/updown_exit_window_mins values.

### C_tighten_high_price_buy_yes_15m (priority 3)
- Why now: High-price (>=0.50) BUY_YES recent losses totaled 5.00.
- Proposed change: `{"xrp_macro.entry_price_max": 0.55, "xrp_macro.entry_window_15m_max": 15.0}`
- Predicted effect: Reduce tail losses from expensive YES entries in 15m windows.
- Success metric: No single XRP BUY_YES loss worse than -$4.00 across next 20 XRP closes.
- Failure trigger: Trade count collapse (>40% drop) with no PnL improvement over same horizon.
- Rollback rule: Restore prior entry_price_max and entry_window_15m_max values.
