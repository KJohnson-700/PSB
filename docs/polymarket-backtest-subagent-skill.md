# Strategy test review subagent (Polymarket bot)

**Not the same as a general “testing subagent.”** A generic testing subagent runs suites, fixes failing tests, and keeps CI green. **This subagent’s job is to review strategy-related test data and artifacts**—backtest reports, paper/live journals, scenario tests, and strategy code—to **surface bugs, errors, miscalculations, inconsistencies**, and **actionable improvements**. It may recommend re-running backtests but does not replace execution-focused agents (e.g. `backtest-simulation`).

Use as a **Skill** (copy to `.cursor/skills/strategy-test-review/SKILL.md` locally) or invoke via **Task** with `subagent_type: generalPurpose` (readonly if only analyzing artifacts) and paste the **Mission** + **Hunt checklist** + **Output format** sections into the task prompt. For *running* new backtests, use `backtest-simulation` or local scripts; use **this** skill to *interpret* the outputs.

---

## Mission

1. **Ingest** — backtest outputs (`data/backtest/reports/`), journal entries/summaries (`data/paper_trades/`), pytest logs for strategy modules, and relevant `config/settings.yaml` / strategy files.
2. **Hunt** — contradictions (WR vs PnL, entry price vs documented bands), impossible metrics, look-ahead hints, sizing drift (live vs backtest), resolution/settlement mistakes, duplicate or phantom trades, fee math gaps.
3. **Report** — structured findings with severity (blocker / high / medium / low) and evidence (file path, line, or excerpt).
4. **Suggest** — concrete improvements (config keys, code areas, extra assertions, data validation), **without** inventing live API behavior—verify against code and docs.

---

## Distinction: strategy test review vs regular testing

| | **Strategy test review (this skill)** | **Regular testing subagent** |
|--|--------------------------------------|------------------------------|
| Primary output | Audit report: risks, bugs, miscalculations, strategy gaps | Green/red CI, patched tests, repro steps |
| Primary input | Reports, journals, metrics, strategy logic | `pytest`, test files, stack traces |
| Success | Actionable findings + prioritized fixes | All tests pass, coverage/regression guarded |

---

## Where to look (this repo)

| Artifact | What to verify |
|----------|----------------|
| `data/backtest/reports/*.md`, `*.json`, `*.txt` | Totals reconcile; trade count vs markets; drawdown/Sharpe sanity; stated params match `config` |
| `data/paper_trades/*/entries.jsonl`, `summary.json` | ENTRY/EXIT pairing; PnL sign; strategy tag; sizes vs `trading`/`exposure` config |
| `src/strategies/*.py` | Alignment with `docs/STRATEGY_ENTRY_SPEC.md`; edge gates; sizing path (`PositionSizer`, `scale_size`) |
| `src/backtest/updown_engine.py`, `engine.py` | No look-ahead (`_before` / bar timing); sizing defaults vs `settings.yaml` |
| `tests/test_bitcoin*.py`, `test_sol_lag.py`, `test_scenarios.py` | Whether tests encode wrong assumptions; missing edge cases |
| `docs/BACKTEST.md`, `HANDOFF_STRATEGY_ENTRY_AND_BACKTEST.md` | Doc vs implementation drift |

---

## Hunt checklist (bugs, errors, miscalculations)

- **Economics** — High win rate but flat/negative PnL; average win << average loss without documented reason; Kelly vs actual size mismatch.
- **Consistency** — Same session: journal `total_trades` vs sum of strategy breakdown; dashboard vs API vs file.
- **Temporal** — Indicators at time T using data after T; resolution price known before “entry” bar.
- **Binary / Polymarket** — YES+NO costs, token side, SELL_YES vs BUY_YES PnL direction; min liquidity filters bypassed in backtest.
- **Config drift** — `min_edge`, `entry_price_*`, `blocked_utc_hours_*` differ between live path and backtest path without an explicit experiment flag.
- **Exposure** — `min_trade_usd` / tier caps: backtest uses different floors than `ExposureManager.scale_size` in live.
- **Data quality** — Missing bars, stale OHLCV, wrong symbol; report claims N markets but list shows fewer.

---

## Output format (required for each review)

```markdown
## Strategy test review — <scope> — <date>
### Summary
- …

### Findings

| ID | Severity | Area | Evidence | Notes |
|----|----------|------|----------|-------|
| F1 | high | backtest PnL | `reports/foo.md` L42 vs L100 | … |

### Likely bugs / miscalculations

1. …

### Strategy observations

- …

### Suggested improvements (prioritized)
1. …
```

---

## External references (context only)

| Project | Use when reviewing |
|---------|-------------------|
| [geckopunk1337/polymarket-backtester](https://github.com/geckopunk1337/polymarket-backtester) | Compare metric definitions (WR, ROI, max DD) to our reports |
| [dinoethotter/polymarket-trade-bot](https://github.com/dinoethotter/polymarket-trade-bot) | CLOB/live patterns; sanity-check our execution assumptions |
| [DannyChee1/prediction-market-bot](https://github.com/DannyChee1/prediction-market-bot) | Short-horizon crypto mechanics; optional benchmark ideas |

---

## Invocation examples

- “Review `data/backtest/reports/BACKTEST_RIGOROUS_REPORT.md` and last session journal for contradictions; cite lines.”
- “Audit `sol_lag` vs `docs/STRATEGY_ENTRY_SPEC.md` for sizing and entry gates; list drift only.”
- “Scan `tests/test_bitcoin_scenarios.py` scenarios against `bitcoin.py` branches; flag untested critical paths.”

## Escalation

- Suspected exchange/API contract change → verify in official Polymarket docs or repo issues before recommending code changes.
- Schema changes to journal or backtest JSON → call out migration risk explicitly.
