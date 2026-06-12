from types import SimpleNamespace

from src.analysis.decision_snapshot import DecisionSnapshot


def test_decision_snapshot_preserves_lane_skip_context():
    snapshot = DecisionSnapshot(
        strategy="bitcoin",
        market_id="m1",
        market_question="Bitcoin Up or Down",
        signal_reason="edge ok",
        lane_meta={
            "lane_id": "bitcoin|5m|up|bull|standard",
            "promotion_state": "paper",
        },
    )

    extra = snapshot.skip_extra(
        skip_reason="unsellable_token",
        dry_run=True,
        matched_rule="bitcoin|5m",
    )

    assert extra == {
        "lane_id": "bitcoin|5m|up|bull|standard",
        "promotion_state": "paper",
        "signal_reason": "edge ok",
        "skip_reason": "unsellable_token",
        "dry_run": True,
        "lane_rule_match": "bitcoin|5m",
    }


def test_decision_snapshot_builds_entry_signal_from_signal_and_lane_meta():
    signal = SimpleNamespace(
        market_id="m1",
        market_question="SOL Up or Down",
        action="BUY_NO",
        direction="down",
        reason="lag follow",
    )

    snapshot = DecisionSnapshot.from_signal(
        strategy="sol_macro",
        signal=signal,
        entry_leg="NO",
        lane_meta={"lane_id": "sol_macro|15m|down|flat|standard"},
    )

    assert snapshot.lane_id == "sol_macro|15m|down|flat|standard"
    assert snapshot.entry_signal({"window_size": "15m"}) == {
        "window_size": "15m",
        "lane_id": "sol_macro|15m|down|flat|standard",
    }


def test_entry_signal_lane_meta_overrides_explicit_fields():
    # Invariant: lane_meta is applied LAST so it wins key collisions, matching the
    # legacy `{...explicit, **lane_meta}` literal the refactor replaced.
    snapshot = DecisionSnapshot(
        strategy="bitcoin",
        market_id="m1",
        market_question="Bitcoin Up or Down",
        lane_meta={"htf_bias": "BEARISH", "lane_id": "bitcoin|5m|down|bear|standard"},
    )

    payload = snapshot.entry_signal({"htf_bias": "BULLISH", "window_size": "5m"})

    assert payload == {
        "htf_bias": "BEARISH",
        "window_size": "5m",
        "lane_id": "bitcoin|5m|down|bear|standard",
    }
