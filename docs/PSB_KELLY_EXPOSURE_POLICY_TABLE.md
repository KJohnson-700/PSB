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
| Global position cap | `risk.max_concurrent_positions` applies to the whole active bot |
| Term budget | `term_risk.caps.<TERM>` caps aggregate open notional for that market term |
| Term budget | Open positions sharing a market term consume the configured cap, e.g. `SHORT_TERM` |

## Action

Map **B**, **D**, and target \$ per trade into **`exposure.*`** and **`max_position_size`** so tier floors/caps match operational intent.
