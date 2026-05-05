# Vault handoff — paste into Hermes Obsidian vault

Copy the blocks below into the paths shown. The vault root in AGENTS is `projects/polymarket-bot/` (relative to your Obsidian vault).

---

## 1. `changelog.md` (infrastructure)

Paste at the **top** of the file (newest first).

```markdown
## 2026-05-04 — Shadow AI pipeline, Discord notifier hot-reload

**Repo:** PSB (`5a65495` area)

- **Shadow / Tier-C AI:** Log-only three-stage shadow flow in `eth_macro` (research narrative → trader → portfolio); config under `ai.shadow_pipeline` + `research_narrative`; artifacts in ops digest and dashboard. No live orders from shadow path.
- **AIAgent:** Centralized YAML extraction (`_extract_ai_config`); safer marginal recommendation coercion; shared schema/render helpers under `src/ai/`.
- **Discord / notifications:** `NotificationManager.reload_from_config()`; `merge_discord_webhook_from_env` applied when dashboard merges config; startup log line `DISCORD STATUS` (webhook present, global enabled, strategies). `xrp_dump_hedge` added to trade/exit title allowlists. Entry `notify_trade` remains intentionally inert for configured strategies (exit-focused alerts per project rules).
- **Tests:** `test_notification_manager`, `test_live_config_apply` (shared fake exposure manager for weather/event aliases), AI parse/schema render, ops pulse, dashboard bundle.

```

---

## 2. `strategy-log/eth_macro.md` — Change Log

Append a new entry at the **top** of the Change Log section (after reading `_index.md` if the template differs).

```markdown
### 2026-05-04 — Shadow AI pipeline (log-only)

| Field | Content |
|-------|---------|
| **What changed** | Tier-C shadow AI: research narrative, trader, portfolio — wired in `eth_macro`, log-only; config in `settings.yaml` (`ai.shadow_pipeline`, `research_narrative`). |
| **Why** | Exercise full multi-agent narrative → decision → sizing path in production logs without execution risk; surface shadow line on ops/dashboard. |
| **Hypothesis** | Structured shadow logs improve post-hoc review and future gating to live AI assist without changing fills. |
| **Expected outcome** | Shadow block in journal/ops when enabled; no change to entry/exit mechanics or sizing from AI. |
| **Actual outcome** | *pending* — needs ≥15 closed trades post-deploy for live validation. |
| **Status** | pending |

```

---

## 3. Not committed (local only)

Working tree may still contain modified `data/backtest/*`, `data/entry_prices/*`, `docs/session_reports/*`, and scripts under `scripts/` — intentionally **not** included in the feature commit. Commit or discard separately if needed.
