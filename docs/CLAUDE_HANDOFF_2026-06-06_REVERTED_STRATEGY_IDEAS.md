# Claude Handoff: Reverted Codex Strategy Ideas

Date: 2026-06-06
Target rollback session: `test_20260604_234611`
Target commit: `b93ad0e`
Target config hash: `209e2f96baa2`

## Operator Direction

The operator requested returning active strategy behavior to the state of `test_20260604_234611`, except for changes that helped `bnb_macro`. Do not reactivate the reverted ideas below without a focused review and explicit operator approval.

## Kept Active

- `bnb_macro` local 5m native BUY_NO guard no longer depends on BTC 1h regime.
- `bnb_macro` config key is BTC-free: `bnb_5m_native_buy_no_max_yes_price`.
- `bnb_macro` has explicit `15m up` exit coverage with hold-winners plus trailing floor.

## Reverted To Notes Only

- Shared alt 1h alignment rewrite that changed neutral handling and expanded fast-window blocks.
- Removal of BTC 1h regime action scoring from `updown_composite_score`.
- Risk manager paper daily-loss enforcement and tiny rounded-size rejection changes.
- Trade journal run-provenance fields.
- BTC 5m fresh-cross override default-off change.
- HYPE Binance HTTP 451 cooldown.

## Reactivated For Narrow Session Trial

- ETH 5m BTC-follow impulse requirement flip: `eth_macro.btc_follow_5m_requires_impulse: true`.
- HYPE native BUY_YES neutral-1h weak-convergence guard.
- XRP 5m native BUY_NO disable only. Neutral-1h BUY_YES suppression was checked against ghosts and left disabled in config.
- SOL and DOGE 5m native BUY_NO reopen.

These were reactivated on 2026-06-06 only as strategy-scoped/config-scoped changes. The broad shared-code/plumbing items above remain inactive.

## Notes For Future Review

- Any future alt strategy change should be scoped to one strategy or one shared invariant at a time.
- If using ghost data, validate the exact lane and then change only that lane/config path.
- Do not bundle observability, execution risk, and signal gates in the same patch.
