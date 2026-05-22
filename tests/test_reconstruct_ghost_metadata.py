from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from tools.reconstruct_ghost_metadata import (
    BtcRegimeLookup,
    build_completed_btc_1h_frame,
    reconstruct_convergence,
    reconstruct_rows,
)


def test_btc_regime_lookup_uses_last_completed_hour_without_lookahead() -> None:
    rows = []
    start = pd.Timestamp("2026-05-20T00:00:00Z")
    for i in range(24 * 4):
        price = 100.0 + (i * 0.1)
        rows.append(
            {
                "open_time": start + pd.Timedelta(minutes=15 * i),
                "close_time": start + pd.Timedelta(minutes=15 * (i + 1)) - pd.Timedelta(milliseconds=1),
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 1.0,
            }
        )
    hourly = build_completed_btc_1h_frame(pd.DataFrame(rows), range_band_pct=0.001)
    lookup = BtcRegimeLookup(hourly)

    found = lookup.lookup(datetime(2026, 5, 20, 21, 1, tzinfo=timezone.utc))

    assert found is not None
    assert found["btc_1h_regime"] == "BULL"
    assert found["btc_1h_regime_ts"] == "2026-05-20T21:00:00+00:00"


def test_reconstruct_convergence_prefers_probe_and_edge() -> None:
    row = {
        "action": "BUY_YES",
        "est_prob_up": 0.62,
        "yes_price": 0.55,
        "effective_min_edge": 0.05,
        "probe_variants": [
            {
                "probe": "lane_min_edge",
                "kind": "baseline",
                "observed": 0.07,
                "threshold": 0.05,
                "margin": 0.02,
                "would_pass": True,
            }
        ],
    }

    out = reconstruct_convergence(row)

    assert out["convergence_score"] is not None
    assert out["convergence_source"] == "probe_edge"
    assert out["convergence_reconstructed"] is True


def test_reconstruct_rows_fills_all_missing_fields_with_fallbacks() -> None:
    rows = [
        {
            "ghost_id": "g1",
            "ts": "2026-05-20T12:00:00+00:00",
            "reason": "oracle_basis_block",
            "action": "BUY_NO",
        }
    ]

    reconstructed, summary = reconstruct_rows(
        rows,
        rejected_by_id={},
        regime_lookup=BtcRegimeLookup(pd.DataFrame()),
    )

    assert summary["missing_btc_1h_regime_after"] == 0
    assert summary["missing_convergence_after"] == 0
    assert reconstructed[0]["btc_1h_regime"] == "RANGE"
    assert reconstructed[0]["convergence_score"] == 0.35
    assert reconstructed[0]["convergence_source"] == "reason_prior"
