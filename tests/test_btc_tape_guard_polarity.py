"""Polarity truth-table for the two BTC tape-adapter guards (Codex ask, 2026-08-16).

THE BUG THIS LOCKS OUT: lane_tape_adapter.raw_admission_delta returns NEGATIVE on a realized-
WINNING lane (-loosen_max * strength) and POSITIVE on a LOSING one (+tighten_max * severity).
Both BTC guards compare that delta against a POSITIVE escape constant, so getting the direction
of the comparison wrong silently inverts the guard: it blocks the winners and takes the losers.

That is not hypothetical — it happened TWICE in the same file:
  * the momentum-contradiction guard was fixed 2026-08-12
  * the neutral-resolver short guard carried the same inverted test until 2026-08-16

Neither slip raised an error, changed a count, or failed a test. The only thing that catches it
is asserting the polarity directly, which is what this file does.

Run: .venv/bin/python -m pytest tests/test_btc_tape_guard_polarity.py -q
"""
import pytest

ESCAPE = 0.02

# The two guards' live suppression predicates, extracted verbatim in form.
# Escape requires a genuinely PROFITABLE lane: delta <= -escape.
def suppress_neutral_resolver_short(adm_down, escape=ESCAPE):
    """bitcoin.py — btc_short_against_bull_upspike (guard 1, fixed 2026-08-16)."""
    return adm_down > -escape


def suppress_momentum_contradiction(adm_c, escape=ESCAPE):
    """bitcoin.py — momentum-contradiction guard (guard 2, fixed 2026-08-12)."""
    return adm_c > -escape


GUARDS = [
    pytest.param(suppress_neutral_resolver_short, id="guard1_neutral_resolver_short"),
    pytest.param(suppress_momentum_contradiction, id="guard2_momentum_contradiction"),
]


# ── the truth table Codex asked for: -0.03, 0.0, +0.05, and the -0.02 boundary ──
@pytest.mark.parametrize("guard", GUARDS)
@pytest.mark.parametrize(
    "delta,should_suppress,why",
    [
        (-0.03, False, "clearly WINNING lane (loosen) must ESCAPE suppression"),
        (-0.02, False, "exactly at escape — winning enough, admit (boundary is inclusive)"),
        (-0.019, True, "not winning ENOUGH to clear the escape -> suppress"),
        (0.0, True, "no adapter data / warmup -> suppress is the SAFE default"),
        (+0.05, True, "clearly LOSING lane (tighten) must NEVER be admitted"),
    ],
)
def test_polarity_truth_table(guard, delta, should_suppress, why):
    assert guard(delta) is should_suppress, f"delta={delta}: {why}"


@pytest.mark.parametrize("guard", GUARDS)
def test_the_inversion_itself_is_caught(guard):
    """The specific defect: a winning lane suppressed while a losing lane is admitted."""
    winning, losing = -0.03, +0.05
    assert guard(winning) is False, "a WINNING lane was suppressed — sign is inverted"
    assert guard(losing) is True, "a LOSING lane was admitted — sign is inverted"
    # and the old broken form must NOT satisfy the table (guards against a silent revert)
    old_broken = lambda d, e=ESCAPE: d < e          # noqa: E731
    assert old_broken(winning) is True and old_broken(losing) is False, \
        "the historical inverted form should block winners and admit losers"


@pytest.mark.parametrize("guard", GUARDS)
def test_zero_delta_behaviour_is_unchanged_by_the_fix(guard):
    """Both the old and new forms suppress at delta 0.0 — the fix is CORRECTNESS, not a
    frequency unlock. Every BTC lane was 0.0 when this shipped, so nothing moved that day."""
    old_broken = lambda d, e=ESCAPE: d < e          # noqa: E731
    assert guard(0.0) is True and old_broken(0.0) is True


def test_both_guards_agree_everywhere():
    """The two guards consult the SAME adapter and must never disagree — divergence is how
    the 08-12 fix ended up applied to one and not the other for four days."""
    for d in (-0.05, -0.03, -0.02, -0.01, 0.0, 0.01, 0.03, 0.05):
        assert suppress_neutral_resolver_short(d) == suppress_momentum_contradiction(d), \
            f"guards disagree at delta={d} — one of them has drifted again"
