# Emergency bleed diagnostic — 2026-08-12

## Scope

Clean baseline window: sessions at/after `test_20260811_205943` (`20260811_2059` PT).

Source: local paper journal `data/paper_trades/*/entries.jsonl`, closed `EXIT` rows only.

## Headline

The bot is negative because the current config combined low-WR entry routes with hold-to-resolution behavior that let high-MFE trades round-trip to zero.

Observed clean window:

| Metric | Value |
|---|---:|
| Closed exits | 118 |
| Net PnL | -$173.26 |
| Losing exits | 77 |
| Gross loss | -$673.67 |

## Main Drivers

| Driver | Closed | Net |
|---|---:|---:|
| RSI-fade routes | 26 | -$116.54 |
| BTC 15m bearish BUY_NO | 5 | -$24.45 |
| ETH 15m BUY_YES | 12 | -$38.35 |
| BNB 15m BUY_YES | 6 | -$36.59 |
| XRP 5m BUY_NO | 10 | -$18.24 |

These entry blocks remove 53 observed exits totaling `-$197.59`. The remaining observed exits net `+$24.33` before any exit improvement.

## Exit Conversion Failure

Losers with MFE `>=30%`:

| Metric | Value |
|---|---:|
| Count | 13 |
| Actual loss | -$131.71 |
| Conservative bank-at-30%-arm estimate | +$38.22 |
| Approximate swing | +$169.93 |

Examples:

| Lane | Entry | MFE | Final PnL |
|---|---:|---:|---:|
| BTC 15m BUY_NO drift | 0.40 | +141% | -$15.65 |
| BTC 15m BUY_NO drift | 0.20 | +358% | -$11.65 |
| BTC 15m RSI-fade BUY_YES | 0.51 | +75% | -$11.39 |
| BTC 1h bearish BUY_NO | 0.40 | +94% | -$6.96 |

## Applied Fix

Config-only emergency patch in `config/settings.yaml`:

- `risk.rsi_fade.enabled: false`
- `trading.exit_rules.tp_giveback_enabled: true`
- `direction.mode: quant`
- `direction.enforce: false`
- `strategies.bitcoin.disable_buy_no_15m_when_bearish: true`
- `strategies.eth_macro.disable_buy_yes_15m: true`
- `strategies.bnb_macro.disable_buy_yes_15m: true`
- `strategies.xrp_macro.disable_buy_no_5m: true`

Live runtime intervention:

- Stopped background writers `scripts/psb_direction_driver.py` and `scripts/ai_direction_engine.py`.
- Cleared `data/runtime/claude_direction_override.json`.
- Restarted the local paper bot outside the sandbox on `127.0.0.1:8082`.

Why this mattered: the running bot was not just "choosing wrong"; background direction writers were actively rewriting the override file and restoring `mode=claude/enforce=true`, causing `DIRECTION_OVERRIDE ... applied=True` lines that forced BTC/BNB long even while BTC HTF was bearish and clean journal data showed those long-side/15m routes were bleeding.

## Operational Note

`risk.rsi_fade.enabled` is hot-reloadable. `tp_giveback_enabled` is read in `LiveTesting.__init__`, so the paper bot must be restarted for the exit-conversion fix to apply.

## Status

Restarted session: `test_20260812_132943`.

First post-fix pulse showed `Daily trades: 0/200`, no positions, and BTC skip reason `buy_no_15m_bearish_disabled` instead of live side-forcing. Pending forward validation: rerun `scripts/lane_review.py --since 20260812_132943` after 30-50 post-restart closed trades.
