# Strategy, AI, execution inventory and freeze RCA

Operator reference: how strategies, AI, and execution connect, plus evidence for “bot froze” incidents. **Do not duplicate strategy tuning here** — see `projects/polymarket-bot/strategy-log/`.

## Hermes vault, pre-HYPE baseline, and Obsidian REST

**Pre-HYPE** operator data (notes, exports, older log narratives) may live only in the **Hermes Second Brain** vault, not in this repo’s `data/logs/`. Typical paths: `Hermes Second Brain/projects/psb/notes/` (see [`docs/OBSIDIAN_LOCAL_REST_API.md`](OBSIDIAN_LOCAL_REST_API.md) for layout). Use those notes when comparing behavior before vs after HYPE scanner work.

To **push or patch vault files from scripts**, set in **local** `.env`: `OBSIDIAN_REST_API_URL`, `OBSIDIAN_API_KEY` (Bearer token from the Local REST API plugin). **Railway does not need these** — the hosted bot does not talk to Obsidian unless you add a custom integration.

**Hosted (Railway) verification:** After deploy, logs must include `Scanner: sync network phase (thread) starting` and `finished in …ms` (see [`docs/RAILWAY.md`](RAILWAY.md)). The Railway service runs the **same** `src/market/scanner.py` as local; there is no separate “Railway-only” build path. If those lines are missing, the deployment is still on an **old image** — redeploy from Git.

## Strategy matrix

| Strategy | `settings.yaml` key | Enabled (default config) | Loop | `scan_and_analyze` | Execution in `main.py` | Exposure manager |
|----------|---------------------|--------------------------|------|---------------------|------------------------|------------------|
| Bitcoin up/down | `strategies.bitcoin` | yes | `_unified_cycle` | `BitcoinStrategy` | `_execute_bitcoin_signal` → `_execute_bitcoin_signal_impl` | `btc_exposure_manager` |
| SOL macro | `strategies.sol_macro` | yes | `_unified_cycle` | `SolMacroStrategy` | `_execute_sol_macro_signal` | `sol_exposure_manager` |
| ETH macro | `strategies.eth_macro` | yes | `_unified_cycle` | `ETHMacroStrategy` | `_execute_sol_macro_signal` | `eth_exposure_manager` |
| HYPE macro | `strategies.hype_macro` | yes | `_unified_cycle` | `HYPEMacroStrategy` | `_execute_sol_macro_signal` | `hype_exposure_manager` |
| XRP macro | `strategies.xrp_macro` | yes | `_unified_cycle` | `XRPMacroStrategy` | `_execute_xrp_macro_signal` | `xrp_exposure_manager` |
| Arbitrage | `strategies.arbitrage` | no | `_unified_cycle` | `ArbitrageStrategy` | `_execute_arbitrage_signal` | `event_exposure_manager` (default) |
| Fade | `strategies.fade` | no | `_unified_cycle` | `FadeStrategy` | `_execute_fade_signal` | `event_exposure_manager` |
| NEH | `strategies.neh` | no | `_unified_cycle` | `NothingEverHappensStrategy` | `_execute_neh_signal` | `event_exposure_manager` |

One scan per `trading.cycle_interval_sec` (default 120s): exits, optional arb/fade/neh, then crypto strategies, then resolution — `src/main.py` `PolyBot._unified_cycle` / `_unified_trading_loop`.

## Discord execution alerts

Only these strategies may emit trade/exit Discord notifications (`src/notifications/notification_manager.py`): `bitcoin`, `sol_macro`, `eth_macro`, `hype_macro`, `xrp_macro`.

## AI integration matrix

| Location | Behavior |
|----------|----------|
| `config/settings.yaml` → `ai.enabled`, `ai.live_inferencing`, `ai.timeout`, `ai.provider_chain` | Master AI config; `live_inferencing: false` skips provider calls early in `AIAgent.analyze_market`. |
| `src/analysis/ai_agent.py` | `analyze_market` uses `asyncio.wait_for(..., timeout=self.timeout)` per provider call; consensus path aggregates providers. |
| `src/strategies/bitcoin.py` | `analyze_market` for marginal edge and updown paths when `use_ai` / `use_ai_updown` and `is_available()`. |
| `src/strategies/sol_macro.py` | Same pattern (ETH/HYPE/XRP inherit via `SolMacroStrategy`). |
| `src/strategies/fade.py` | `analyze_market` when `use_ai` and AI available. |
| `src/strategies/arbitrage.py` | `use_ai` flag; optional `analyze_market` on paths gated in code. |
| `src/strategies/neh.py` | Holds `AIAgent`; NEH signal path is quant/filter-based (no `analyze_market` in the main scan loop). |
| `src/backtest/backtest_ai.py` | `BacktestAIAgent` for backtests — not live providers. |

## Execution triggers

| Trigger | Where |
|---------|--------|
| **Dry run** | `trading.dry_run` in YAML; CLI `--paper` / `--live` merges in `src/main.py` (`_parse_run_args`). |
| **Kill switch** | File `data/KILL_SWITCH` — blocks new trades when present (`src/main.py`). |
| **CLOB orders** | `src/execution/clob_client.py` — `place_order` returns synthetic fill when `dry_run`; live uses executor for sync CLOB client. |
| **Sell pre-check** | `can_sell_token` — skipped when dry run. |
| **Resolution / settle** | `ResolutionTracker` from `_run_resolution_check` under `asyncio.Lock` (`_execution_lock`). |
| **Per-lane loss kill** | `ExposureManager` + `trading.loss_kill_switch_enabled` (separate streaks per crypto lane). |

## Freeze forensics (local log sample)

**Repository log:** `data/logs/polybot_20260421.log` (no separate archived “pre-HYPE” log in-tree).

**Git history:** This checkout has a single initial commit containing `fetch_hype_alt_updown_markets`; there is no older commit in-repo to diff for a “before HYPE” baseline.

**Log gap analysis (timestamps):**

1. **~900s gap (16:21 → 16:36):** Not a hang — graceful **shutdown** after `HYPEMacroStrategy` `AttributeError` (missing `scan_and_analyze`), then a **new process** start. Matches the April 2026 fix notes in `projects/polymarket-bot/changelog.md`.
2. **~144s gap during an active fast cycle (21:19:57 → 21:22:30):** Scanner logs show Gamma + 15m/5m updown fetches completing by **21:20:06**, then **“Fetched 51 Hyperliquid/HYPE alt up/down markets”** at **21:22:30** — **~2.5 minutes** inside synchronous HYPE alt slug HTTP. During that window the asyncio event loop was blocked (sync `requests` inside `async def scan_for_opportunities`), so **both** the main loop and fast loop stall — consistent with a “frozen” bot.

## Hardening implemented (this doc’s engineering follow-up)

1. **Thread offload:** Gamma + updown + optional HYPE alt HTTP runs in `asyncio.to_thread` so the event loop stays responsive.
2. **Budget:** `polymarket.scanner_sync_timeout_sec` (default **120**) wraps the threaded phase in `asyncio.wait_for`; on timeout the scanner returns an empty structured result and logs `sync_phase_timeout` in `scanner_meta`.
3. **HYPE alt gate:** HYPE alt markets are fetched only when `strategies.hype_macro.enabled` is true, unless `polymarket.fetch_hype_alt_markets` is set to force on/off.
4. **Heartbeat:** Logs `Scanner: sync network phase (thread) starting/finished in Nms` and `Scanner: scan_for_opportunities complete in Nms`.

See `src/market/scanner.py` and `config/settings.yaml` (`scanner_sync_timeout_sec`, optional `fetch_hype_alt_markets`).
