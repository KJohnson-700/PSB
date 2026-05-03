# Paper vs live friction (sizing realism)

Paper sessions validate **logic and gates**; they **understate** friction versus live Polymarket.

## Spread

- A **\$10** position in a **2¢** wide market pays roughly **\$0.20** round-trip in spread alone (**~2%** of notional) before directional edge — scales with tight books.

## Settlement / resolution lag

- Live: **30–90 seconds** (typical observation band; venue-dependent) between fill and binary resolution on short windows — not modeled as drag in paper Kelly paths.

## Slippage

- Live orders may walk the book; paper often assumes mid or last.

## Recommendation

- Treat paper PnL as **relative** (before/after code changes in the same simulator).
- Before sizing up live, stress-test assumptions in docs + optional wider **`min_edge`** in live config.
