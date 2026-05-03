# Kelly and exposure policy table (PSB codebase)

Values below are **defaults / mechanics** — tune via `config/settings.yaml`.

## KellySizer (`src/analysis/kelly_sizer.py`)

| Mechanism | Behavior |
|-----------|-----------|
| Per-strategy **`AssetKellyConfig`** | `base_kelly_fraction`, streak multiplier, `min_kelly_fraction` |
| **`size_from_edge`** | `base_size = edge * frac * bankroll`, then **`min(..., bankroll * 0.05)`** — **5% hard cap per sizing call** |
| **`size_binary_position`** | Full Kelly × fractional Kelly × bankroll, same **5% cap** |
| **`trading.kelly_fraction`** | Global fraction reload |

Override per strategy with **`strategies.<name>.kelly_fraction`** when present.

## ExposureManager (`src/execution/exposure_manager.py`)

| Tier | Role |
|------|------|
| FULL / MODERATE / MINIMAL | Multipliers on sized USD; tier caps (~\$15 / \$13 / \$10 defaults) |
| PAUSED | Kill switch path |

## RiskManager (`src/execution/clob_client.py`)

| Control | Default idea |
|---------|----------------|
| Crypto vs non-crypto pools | Separate concurrency and budget |
| **`CRYPTO_MAX`** | Max concurrent crypto positions |
| Short-term crypto budget | Fraction of bankroll vs open SHORT_TERM crypto cost |

## Action

Map **B**, **D**, and target \$ per trade into **`exposure.*`** and **`max_position_size`** so tier floors/caps match operational intent.
