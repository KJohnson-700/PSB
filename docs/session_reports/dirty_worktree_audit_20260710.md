## Dirty Worktree Audit — 2026-07-10

### Scope

Audited unstaged tracked files plus untracked files present before a possible VPS restart/deploy. No VPS process was restarted or changed during this audit.

### Decision Summary

| Group | Files | Decision | Why We Thought We Needed It | Suspected Outcome | Deploy Risk |
|---|---|---|---|---|---|
| P0 staged package | `config/settings.yaml`, `src/execution/clob_client.py`, `src/execution/live_testing.py`, `src/main.py`, `tests/test_buy_yes_lane_repair.py`, `tests/test_clob_client_hardening.py`, `tests/test_live_config_apply.py`, `tests/test_updown_exit_shared.py` | Keep staged | Hot-reload config/exit knobs, realistic paper entry fills, BTC 1h UP trail exemption, XRP 15m BUY_YES disable | Avoid restart for config-only changes; make paper entries closer to smoke-test fills; stop leaking XRP 15m LONG losses | Low after focused tests |
| Websocket market-channel fix | `src/market/websocket.py`, `tests/test_websocket_market_channel.py` | Keep and stage | Polymarket market websocket events can arrive as `event_type`, use `asset_id`, send full `book` snapshots, and send `price_change` arrays. Old parser only handled `type`/`token_id` and weak price updates. | Fresher order-book cache, better scanner/dashboard websocket pricing, fewer stale/missing book updates | Moderate but tested; keep paired with test |
| Secret hygiene | `.gitignore`, `AGENTS.md`, `scripts/tracked_secret_sweep.py` | Quarantine for separate review | Prevent secret/key files from entering repo and document hard handling rules | Safer local ops | Low runtime impact, but not needed for trading deploy |
| AI decision strict parser | `src/analysis/ai_agent.py`, `tests/test_ai_agent_parse.py` | Quarantine | Stop malformed AI confidence/probability from being coerced into real decisions | Cleaner AI contract, fewer bogus AI approvals | High frequency risk: changes marginal/veto-only behavior from permissive veto semantics into stricter approval requirements |
| BTC async AI broker/grace | `src/analysis/ai_decision_broker.py`, `src/strategies/bitcoin.py`, `src/main.py`, `config/settings.yaml` unstaged AI keys | Quarantine | Avoid 16-90s AI blocking while preventing historical broker pending-state trade drops | More stable scan cadence with AI still used when quick | High strategy risk: changes BTC admission timing and fail-open/fail-closed behavior |
| Alt AI gate reopen | `src/strategies/sol_macro.py`, `tests/test_sol_macro.py`, `tests/test_alt_btc_decoupling.py` | Quarantine/reject for now | Config had alt AI enabled but inherited gate windows were empty; edit tried to let 15m/1h alt AI fire | More AI tie-breaker coverage on alts | High frequency/profit risk: can suppress or delay SOL/ETH/HYPE/XRP/BNB/DOGE lanes; conflicts with current preference not to tighten without evidence |
| 5m BUY_YES timing dead-zone | `config/settings.yaml`, `src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`, strategy-log files | Quarantine | Timing review suggested first ~75s of 5m BUY_YES bled while later buckets improved | Reduce early-window wrong-side entries | High restriction risk: directly removes trades and needs ghost/live support before deploy |
| Regime fade lane key | `src/analysis/regime_fade.py` | Quarantine | Fade state was strategy-wide, possibly idling too much when only one `(strategy, window, side)` lane was bad | More granular fade gating | Medium strategy risk; needs dedicated tests/data |
| Trade journal session filtering/pruning | `src/execution/trade_journal.py`, `tests/test_trade_journal_resumable.py` | Quarantine | Hide noisy short completed sessions and add prune helper | Cleaner dashboard/session lists | Medium ops risk; can hide evidence if wrong |
| Start supervisor split-process rewrite | `start.py`, `docs/LOCAL_BOT_RUN.md`, `tests/test_start_supervisor.py` | Quarantine/reject for current VPS work | Keep dashboard alive while bot restarts, change local default to split processes | Better local supervision | High ops risk: port changed to 8082 and process model changed; unrelated to VPS P0 |
| Olympus smoke sizing denominator | `src/execution/olympus_client.py` | Quarantine | Keep live smoke order size stable even if paper max position size grows | Safer smoke/live sizing separation | Low/medium but not needed for paper deploy |
| Polymarket taker fee helper | `src/execution/fill_sim.py`, `tests/test_fill_sim.py` | Keep as dependency repair | `src/execution/live_testing.py` already imports and calls `polymarket_taker_fee_usdc` in HEAD, but `fill_sim.py` did not define it after cleanup | Restore import/runtime consistency; no fee applies while `execution_fees.enabled: false` | Low; pure helper with unit test |
| Journal learning strategy keys | `src/analysis/journal_learning.py`, `tests/test_journal_learning_strategy_keys.py` | Quarantine | Include DOGE/BNB strategy keys in learning/config surfaces | Better learning visibility for new alts | Low runtime risk, separate concern |
| Dashboard/status test additions | `tests/test_dashboard_status_startup_session.py`, `tests/test_risk_manager_hardening.py` | Quarantine | Guard startup-session and UTC daily reset behavior | Better ops correctness | Test-only, but not part of P0 |
| Docs/changelogs | `docs/ACTIVE_RECOMMENDATIONS.md`, `docs/AGENT_CHANGELOG.md`, `projects/polymarket-bot/changelog.md`, `projects/polymarket-bot/strategy-log/*.md` | Quarantine | Capture prior proposed changes and strategy-log notes | Better paper trail | Can encode stale recommendations; not runtime but should not be bundled blindly |
| Research/audit artifacts | `.audit_momentum_session.txt`, `.codex_15m_analysis.txt`, `.eth_pockets.txt`, `.firecrawl/**`, `.hermes/**`, untracked docs/session reports/research, local audit scripts | Quarantine | External research, second-brain handoffs, one-off audits, smoke scripts | Useful reference material | Not runtime, but clutters deploy state |
| Config backup/local files | `config/settings.local-fade.yaml`, `config/settings.yaml.bak_*` | Quarantine | Local experiment and rollback snapshots | Reference/rollback only | Must not deploy accidentally |

### Keep Details

#### Websocket Market-Channel Fix

Evidence:
- Existing docs/changelog already mention Polymarket market websocket shape: `/ws/market`, payload key `assets_ids`, event objects, and CLOB order-book cache.
- New test file `tests/test_websocket_market_channel.py` covers:
  - `event_type: book`
  - `asset_id` instead of `token_id`
  - snapshot replacement
  - `price_change` delta merge
  - documented `price_changes` array form
- Focused run passed with P0 suite: `95 passed`.

Decision: keep and stage with P0, because bad websocket parsing can make book cache stale and hurt scanner/dashboard price quality.

### Quarantine Rationale

The quarantined changes are not necessarily useless. Most have a plausible reason. The problem is they change admission, timing, AI, exits, or process supervision without being part of the current baseline-vs-VPS P0 package. The safest deploy candidate is staged P0 plus websocket parser fix, with everything else recoverable from stash.

### Cleanup Plan Applied

1. Stage keepers: P0 package, websocket parser/test, this audit note.
2. Stash remaining unstaged/untracked work with `git stash push --keep-index -u`.
3. Re-run focused tests against the clean deploy candidate.

### Revisit Queue

1. Re-evaluate BTC async AI broker/grace only from cycle-duration logs plus trade admission deltas.
2. Re-evaluate alt AI gate reopen only if ghost/live evidence shows AI improves 15m/1h without starving trades.
3. Re-evaluate 5m BUY_YES dead-zone from settled ghosts/live journal, per asset and window.
4. Re-evaluate start supervisor split-process locally only, not in the VPS trading fix path.
5. Keep websocket parser fix unless live WSS logs prove current event shape differs.
