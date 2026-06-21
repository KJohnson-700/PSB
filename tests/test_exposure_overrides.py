"""Tests for disk-backed exposure pause/resume overrides (split-mode controls)."""
import pytest

from src.execution import exposure_overrides as eo


def _p(tmp_path):
    return tmp_path / "exposure_overrides.json"


def test_normalize_lane_aliases():
    assert eo.normalize_lane("hype") == "HYPE"
    assert eo.normalize_lane("hype_macro") == "HYPE"
    assert eo.normalize_lane("BTC") == "BTC"
    assert eo.normalize_lane("bitcoin") == "BTC"
    assert eo.normalize_lane("bnb_macro") == "BNB"
    assert eo.normalize_lane("nonsense") is None
    assert eo.normalize_lane(None) is None


def test_read_missing_file_is_safe(tmp_path):
    ov = eo.read_overrides(_p(tmp_path))
    assert ov["global_paused"] is False
    assert ov["paused_lanes"] == []


def test_set_global_roundtrip(tmp_path):
    p = _p(tmp_path)
    eo.set_global(True, p)
    assert eo.read_overrides(p)["global_paused"] is True
    eo.set_global(False, p)
    assert eo.read_overrides(p)["global_paused"] is False


def test_set_lane_pause_resume(tmp_path):
    p = _p(tmp_path)
    eo.set_lane("hype", True, p)
    eo.set_lane("bnb_macro", True, p)
    ov = eo.read_overrides(p)
    assert ov["paused_lanes"] == ["BNB", "HYPE"]
    eo.set_lane("HYPE", False, p)
    assert eo.read_overrides(p)["paused_lanes"] == ["BNB"]


def test_set_lane_bad_lane_raises(tmp_path):
    with pytest.raises(ValueError):
        eo.set_lane("xyz", True, _p(tmp_path))


def test_lane_is_paused_respects_global_and_lane(tmp_path):
    p = _p(tmp_path)
    eo.set_lane("hype", True, p)
    ov = eo.read_overrides(p)
    assert eo.lane_is_paused("HYPE", overrides=ov) is True
    assert eo.lane_is_paused("BTC", overrides=ov) is False
    # global pause overrides everything
    eo.set_global(True, p)
    ov2 = eo.read_overrides(p)
    assert eo.lane_is_paused("BTC", overrides=ov2) is True
    assert eo.lane_is_paused("SOL", overrides=ov2) is True


def test_reconcile_semantics_match_managers(tmp_path):
    """Simulate the bot's reconcile loop against fake managers."""
    p = _p(tmp_path)

    class FakeMgr:
        def __init__(self, name):
            self.lane_name = name
            self._manual_pause = False
            self.calls = []

        def manual_pause(self):
            self._manual_pause = True
            self.calls.append("pause")

        def manual_resume(self):
            self._manual_pause = False
            self.calls.append("resume")

    mgrs = [FakeMgr(n) for n in eo.CANONICAL_LANES]
    eo.set_lane("hype", True, p)

    def reconcile():
        ov = eo.read_overrides(p)
        for m in mgrs:
            desired = eo.lane_is_paused(m.lane_name, overrides=ov)
            if desired and not m._manual_pause:
                m.manual_pause()
            elif not desired and m._manual_pause:
                m.manual_resume()

    reconcile()
    hype = next(m for m in mgrs if m.lane_name == "HYPE")
    btc = next(m for m in mgrs if m.lane_name == "BTC")
    assert hype._manual_pause is True
    assert btc._manual_pause is False
    # idempotent: a second reconcile makes no new calls
    hype.calls.clear()
    reconcile()
    assert hype.calls == []
    # resume the lane -> reconcile flips it back
    eo.set_lane("hype", False, p)
    reconcile()
    assert hype._manual_pause is False
