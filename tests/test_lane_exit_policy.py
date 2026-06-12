from src.analysis.lane_exit_policy import (
    drift_signature,
    format_drift_message,
    _recommended_params,
)


def _lane(name, rec_hold, live_hold, gap=10.0, n=30, held=55, real=30):
    return {
        "lane": name,
        "n": n, "held_wr": held, "realized_wr": real, "gap": gap,
        "recommended": {"updown_hold_winners_to_resolution": rec_hold},
        "live_hold_winners": live_hold,
    }


def test_format_drift_message_empty():
    assert format_drift_message([]) == ""


def test_format_drift_message_renders_lane_and_direction():
    msg = format_drift_message([_lane("xrp_macro|5m|BUY_YES", rec_hold=False, live_hold=True)])
    assert "xrp_macro|5m|BUY_YES" in msg
    assert "data wants tight TP/SL" in msg
    assert "live is hold+trail" in msg


def test_neutral_positive_gap_recommends_hold_trail():
    params = _recommended_params("- neutral / watch", gap=54.28)

    assert params["updown_hold_winners_to_resolution"] is True
    assert params["updown_trail_arm_pct"] == 0.10
    assert params["updown_trail_gap_pct"] == 0.15


def test_entry_broken_positive_gap_stays_tight():
    params = _recommended_params("C entry-broken (not an exit fix)", gap=99.0)

    assert params == {"updown_hold_winners_to_resolution": False}


def test_drift_signature_dedup_and_flip():
    a = [_lane("L1", rec_hold=False, live_hold=True)]
    b = [_lane("L1", rec_hold=False, live_hold=True)]  # same -> same sig
    assert drift_signature(a) == drift_signature(b)
    # recommendation flips -> signature changes -> would re-alert
    c = [_lane("L1", rec_hold=True, live_hold=False)]
    assert drift_signature(a) != drift_signature(c)
    # new lane entering drift -> signature changes
    d = a + [_lane("L2", rec_hold=False, live_hold=True)]
    assert drift_signature(a) != drift_signature(d)
