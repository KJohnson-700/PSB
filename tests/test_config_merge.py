"""Tests for shared config deep-merge (dashboard + live bot)."""

import pytest

from src.config_merge import deep_merge_config


def test_deep_merge_rejects_unknown_top_key():
    base = {"ai": {"enabled": True}}
    with pytest.raises(ValueError, match="Unknown config key"):
        deep_merge_config(base, {"not_allowed": 1})


def test_deep_merge_nested():
    base = {"strategies": {"fade": {"enabled": True, "use_ai": True}}}
    deep_merge_config(base, {"strategies": {"fade": {"use_ai": False}}})
    assert base["strategies"]["fade"]["enabled"] is True
    assert base["strategies"]["fade"]["use_ai"] is False

