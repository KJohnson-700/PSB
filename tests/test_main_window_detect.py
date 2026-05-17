from src.main import _detect_window_from_question


def test_detect_window_from_hourly_question():
    q = "Bitcoin Up or Down - May 17, 1AM ET"
    assert _detect_window_from_question(q) == "1h"


def test_detect_window_from_legacy_thirty_minute_range():
    q = "Bitcoin Up or Down - April 21, 1:30AM-2:00AM ET"
    assert _detect_window_from_question(q) == "30m"
