# Backtest rebuild – status & to-do

```
[###################-----] 3/3  Rebuild → Validate → Quick backtest
```

**Latest:** 2024 list **expanded** (10k events → 4861 after filter, 3007 slots). **Both strategies rerun** in progress (60 slugs, status in `RUN_STATUS.md`).

**Status bar:** Tasks completed as we go. Refresh this file to see updates.

---

## To-do

- [x] **Run rebuild (end-year 2024)** — done (4708 events, 3001 market slots → `config/backtest_markets.yaml`)
- [x] **Validate slugs** — running or done: `python scripts/run_backtest_rigorous.py --validate 20` (check terminal)
- [ ] **Quick backtest** — run when ready: `python scripts/run_backtest_rigorous.py --quick --save-report --status-file data/backtest/reports/RUN_STATUS.md`

---

## Preview / notes

- Rebuild finished successfully. Next: run `--validate 20` to confirm 2024 slugs have data.
- Use `--quick` for faster stress runs (25 slugs/strategy).
- **Open this file in a second pane** and re-open or refresh to see the status bar and checkboxes update.

---

## Test run status bar

When you run the rigorous backtest, use `--status-file` to get a **live status bar in another file**:

```bash
python scripts/run_backtest_rigorous.py --quick --save-report --status-file data/backtest/reports/RUN_STATUS.md
```

Open `data/backtest/reports/RUN_STATUS.md` in a second pane; it updates after each run (strategy × period × stress) with a bar like `[████████░░░░░░░░] 8/24 33%` and the current run info.
