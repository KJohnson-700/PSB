from src.analysis.lane_exit_audit import classify


def test_policy_A_exit_kills_edge():
    # held-WR good, big positive gap, realized far below held -> hold+trail
    assert classify(held_wr=65, realized_wr=42, gap=83.1).startswith("A")
    assert classify(held_wr=50, realized_wr=30, gap=38.3).startswith("A")


def test_policy_B_exit_is_engine():
    # negative gap (realized beats held) and entry not broken -> keep tight TP/SL
    assert classify(held_wr=48, realized_wr=48, gap=-76.0).startswith("B")
    assert classify(held_wr=56, realized_wr=52, gap=-13.3).startswith("B")


def test_policy_C_entry_broken_takes_precedence_over_B():
    # held-WR 17% is fundamentally broken at entry even when the exit mitigates
    # (negative gap). C must win over B so the diagnosis stays honest.
    assert classify(held_wr=17, realized_wr=22, gap=-41.3).startswith("C")
    assert classify(held_wr=27, realized_wr=29, gap=2.1).startswith("C")


def test_borderline_held_wr_is_not_A():
    # 47.5% held just under the coin-flip A-bar -> not a slam-dunk hold lane
    assert not classify(held_wr=47.5, realized_wr=38, gap=16.7).startswith("A")
