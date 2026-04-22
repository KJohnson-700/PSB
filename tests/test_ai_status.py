"""Tests for src/ai_status.py."""

import pytest

from src.ai_status import compute_ai_status, format_ai_log_line


def test_compute_ai_disabled():
    cfg = {"ai": {"enabled": False, "provider_chain": []}}
    st = compute_ai_status(cfg, {})
    assert st["enabled"] is False
    assert st["ready"] is False
    assert "llm off" in st["reason"].lower()
    assert "ai.enabled" in st["reason"].lower()


def test_compute_ai_ready_with_keys():
    cfg = {
        "ai": {
            "enabled": True,
            "provider_chain": [
                {"provider": "openrouter", "model": "x", "api_key_secret": "OPENROUTER_API_KEY"}
            ],
        }
    }
    keys = {"OPENROUTER_API_KEY": "sk-test"}
    st = compute_ai_status(cfg, keys)
    assert st["ready"] is True
    assert st["chain_count"] == 1


def test_format_ai_log_line():
    st = {"ready": True, "reason": "ok", "chain_count": 1, "missing_keys": []}
    line = format_ai_log_line(st)
    assert "AI STATUS: ON" in line
    assert "1 provider" in line
