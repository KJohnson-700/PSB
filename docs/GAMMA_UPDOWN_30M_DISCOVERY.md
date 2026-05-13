# Gamma: discovering true 30-minute crypto Up/Down markets

## Bulk `/markets` pagination caveat (2026-05-13)

When scanning **`GET /markets?active=true&closed=false`** (paginated, e.g. `limit=500&offset=…`), **do not** treat a slug as “30m up/down” because the slug contains both the substrings **`updown`** and **`"30"`**.

Unix slugs for **5m** windows often end with timestamps whose decimal digits include **`30`** (e.g. `btc-updown-5m-176616**300**0`, `…**330**0`, `…**130**0`). A naive filter (`"updown" in slug and "30" in slug`) produced **12** false positives over **~8000** active rows — **all were `*-updown-5m-*`**, not half-hour products.

**Use instead:** explicit slug tokens such as **`updown-30m-`** (or the human compact ET range slugs the scanner builds), and/or **`window_minutes`** after parsing — not a bare **`30`** substring on the full slug.

## How the scanner aligns with this

`MarketScanner.fetch_updown_30m_markets` uses **`{asset}-updown-30m-{unix}`** slugs from **`_iter_updown_event_slugs(step_minutes=30, …)`**, optional **human compact** ET slugs, plus merge of **~30m** rows from the **15m** slug batch when **`polymarket.updown_30m_merge_from_15m_slug_batch`** is enabled — see `src/market/scanner.py` and `docs/AGENT_CHANGELOG.md` (2026-05-13).
