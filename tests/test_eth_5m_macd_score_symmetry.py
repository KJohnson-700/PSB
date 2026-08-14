"""ETH 5m MACD confirmation score — side symmetry.

Regression test for the 2026-08-14 fix (Codex GO-WITH-CHANGES, Option B).

THE BUG: `_eth_5m_macd_score` gave SHORT its 0.04 confirmation on
`hist < 0 and not histogram_rising`, but demanded `hist > 0 and histogram_rising`
for LONG. `histogram_rising` is `curr_hist > prev_hist` (STRICT), so a FLAT
histogram reads False — which SHORT's `not rising` accepts and LONG's `rising`
rejects. The true mirror of "positive and rising" is "negative and falling".

Measured consequence over one live day on eth_macro 5m: `eth_5m_weak_confirm`
rejected LONG 2.5x more often than SHORT (509 vs 201), the candidate pool was
58.9% LONG-favorite, and the book still went 85% BUY_NO.

The score is NOT admission-only — it also feeds est_prob_up, `confidence`, and
through those the edge and Kelly sizing — so an asymmetry here is an asymmetry in
sizing too. Keep these cases passing.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.strategies.eth_macro import ETHMacroStrategy


@dataclass
class FakeMACD:
    """Minimal stand-in for MACDResult — the scorer only reads these three."""
    histogram: float
    histogram_rising: bool
    crossover: str = "NONE"


score = ETHMacroStrategy._eth_5m_macd_score

# Live value of eth_follow_5m_min_adj; 0.04 passes, 0.0 rejects.
MIN_ADJ = 0.02


# ── THE FIX: a FLAT histogram must confirm on BOTH sides, not just SHORT ─────────
def test_flat_positive_histogram_confirms_long():
    """The regression. hist > 0, not rising (flat) previously scored 0.0 and was
    rejected as eth_5m_weak_confirm — while its SHORT mirror scored 0.04."""
    s, _ = score(FakeMACD(histogram=0.5, histogram_rising=False), "LONG")
    assert s == pytest.approx(0.04)
    assert s >= MIN_ADJ, "flat-positive LONG must clear the admission floor"


def test_flat_negative_histogram_still_confirms_short():
    """The SHORT side is deliberately UNCHANGED — Option A (dropping `not rising`
    here too) was rejected: it would admit a RECOVERING downtrend on a side that
    already carries b=0.77 / 56.4% breakeven."""
    s, _ = score(FakeMACD(histogram=-0.5, histogram_rising=False), "SHORT")
    assert s == pytest.approx(0.04)
    assert s >= MIN_ADJ


def test_sides_score_equally_on_mirrored_flat_evidence():
    """The symmetry property itself, stated directly."""
    long_s, _ = score(FakeMACD(histogram=0.5, histogram_rising=False), "LONG")
    short_s, _ = score(FakeMACD(histogram=-0.5, histogram_rising=False), "SHORT")
    assert long_s == short_s


# ── everything else must be byte-identical to the pre-fix behaviour ─────────────
def test_rising_positive_still_confirms_long():
    s, _ = score(FakeMACD(histogram=0.5, histogram_rising=True), "LONG")
    assert s == pytest.approx(0.04)


def test_crossover_still_outranks_histogram():
    assert score(FakeMACD(0.5, False, "BULLISH_CROSS"), "LONG")[0] == pytest.approx(0.06)
    assert score(FakeMACD(-0.5, False, "BEARISH_CROSS"), "SHORT")[0] == pytest.approx(0.06)


def test_opposing_histogram_still_penalised():
    assert score(FakeMACD(-0.5, False), "LONG")[0] == pytest.approx(-0.05)
    assert score(FakeMACD(0.5, True), "SHORT")[0] == pytest.approx(-0.05)


def test_recovering_downtrend_still_rejected_on_short():
    """Option A would have made this 0.04. It must stay 0.0 (below MIN_ADJ)."""
    s, _ = score(FakeMACD(histogram=-0.5, histogram_rising=True), "SHORT")
    assert s == pytest.approx(0.0)
    assert s < MIN_ADJ


def test_zero_histogram_confirms_neither_side():
    """hist == 0 is genuinely no evidence; it matches no clause on either side."""
    assert score(FakeMACD(0.0, False), "LONG")[0] == pytest.approx(0.0)
    assert score(FakeMACD(0.0, False), "SHORT")[0] == pytest.approx(0.0)
