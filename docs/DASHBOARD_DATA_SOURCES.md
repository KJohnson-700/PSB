# Dashboard data sources and paper session truth

## Session ID and `entries.jsonl` (heatmap / analytics)

- The **active session** is chosen at bot startup in `src/main.py` via `PAPER_SESSION_ID`, `PAPER_RESUME_SESSION`, or a default fresh `test_%Y%m%d_%H%M%S` — see [RAILWAY.md](RAILWAY.md) **Paper sessions and test data**.
- **`entries.jsonl`** is append-only: **ENTRY** / **EXIT** (and related) events. The dashboard and `scripts/hourly_heatmap.py` depend on it for per-strategy, per-hour analysis. If the directory has only `snapshots.jsonl` and `summary.json` but **no** `entries.jsonl` (or no EXIT lines), you will see **no closed trades** in analytics even when the service is “healthy.”
- **`test_*` resume pitfall** — with `PAPER_RESUME_SESSION=true`, the journal may reattach to an older `test_*` folder if it is the newest on disk. Rename, archive, or clear unwanted session dirs before resuming. Stray `test_*` dirs are called out in [projects/polymarket-bot/changelog.md](../projects/polymarket-bot/changelog.md).
- **After manual edits** to `entries.jsonl`, call `POST /api/journal/invalidate-cache` or use the Live tab **Restart / refresh data** (see below).

## API vs disk vs UI

| What you see | Source | Notes |
|----------------|--------|--------|
| Session stats (PnL, wins, open count, strategy breakdown) | `GET /api/journal/summary` | Prefers `TradeJournal.get_summary()` (`summary_source: live_journal`). Falls back to `data/paper_trades/<session>/summary.json` (`summary_json`) if the journal cannot load. |
| Bankroll in Command Center | `GET /api/status` → `bankroll` | Live: in-memory bot bankroll. **Bot not running:** last `bankroll` field from `entries.jsonl` tail, or `initial_bankroll + total_pnl` from journal summary so the hero is not stuck at `$0`. |
| Closed trades list | `GET /api/journal/trades` | Same journal object as summary (when loadable). |
| BTC chart trade markers | `GET /api/journal/trade-points` | Closed trades only. Marker **time** = **entry** (`opened_at`) for learning; `closed_at` returned separately. Label shows PnL and a small `·in` hint when exit time differs from entry. |
| Equity / PnL line chart | `GET /api/journal/snapshots` | Reads `snapshots.jsonl` for the active session; hidden if fewer than two points. |
| Sabre BULL/BEAR markers | Computed in the browser on fetched OHLCV | Not from the journal. |

## Ground truth before trusting the UI

1. Open `data/paper_trades/<session_id>/summary.json` (legacy snapshot on disk).
2. Compare to `entries.jsonl` (EXIT lines) and `positions.json` (opens).
3. With the bot running, `summary_source: live_journal` should match the journal object used for trades.

## Stale journal / Command Center

- `POST /api/journal/invalidate-cache` clears the dashboard’s in-memory `TradeJournal` cache (mtime-based). Use the **Restart / refresh data** button on the Live tab or call this after manual edits to `entries.jsonl` so the next `GET /api/journal/*` replays from disk.
- Hosted backtests and scans inherit **full** `os.environ` from the bot process (provider keys must be present). A previous denylist that matched `*API_KEY*` broke `OPENROUTER_API_KEY` / `POLYMARKET_API_KEY` in child processes.

## Restart checklist

- [ ] `trading.dry_run` matches intent (paper vs live).
- [ ] No `data/KILL_SWITCH` unless you want trading blocked.
- [ ] `ai.enabled` / per-strategy `use_ai` and `strategies.*.enabled` as intended.
- [ ] Copy `config/settings.yaml` and `config/secrets.env` to a safe backup (do not commit secrets).
