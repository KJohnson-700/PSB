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


def test_compute_ai_ready_primary_key_only_fallback_missing():
    """MiniMax (or any first provider) suffices; later chain entries need not have keys."""
    cfg = {
        "ai": {
            "enabled": True,
            "provider_chain": [
                {"name": "minimax", "type": "minimax", "api_key_secret": "MINIMAX_API_KEY"},
                {"name": "or_", "type": "openai", "api_key_secret": "OPENROUTER_API_KEY"},
            ],
        }
    }
    keys = {"MINIMAX_API_KEY": "sk-cp-xx", "OPENROUTER_API_KEY": ""}
    st = compute_ai_status(cfg, keys)
    assert st["ready"] is True
    assert "OPENROUTER_API_KEY" in st["missing_keys"]


def test_compute_ai_ready_local_provider_without_keys():
    cfg = {
        "ai": {
            "enabled": True,
            "provider_chain": [
                {"name": "ollama_local", "type": "openai", "local": True},
            ],
        }
    }
    st = compute_ai_status(cfg, {})
    assert st["ready"] is True


def test_compute_ai_ready_kimi_coding_oauth(tmp_path):
    creds = tmp_path / "kimi-code.json"
    creds.write_text("{}", encoding="utf-8")
    cfg = {
        "ai": {
            "enabled": True,
            "provider_chain": [
                {
                    "name": "kimi_coding",
                    "type": "kimi_coding",
                    "credentials_path": str(creds),
                },
            ],
        }
    }
    st = compute_ai_status(cfg, {})
    assert st["ready"] is True
    assert st["missing_keys"] == []


def test_compute_ai_missing_kimi_coding_oauth(tmp_path):
    cfg = {
        "ai": {
            "enabled": True,
            "provider_chain": [
                {
                    "name": "kimi_coding",
                    "type": "kimi_coding",
                    "credentials_path": str(tmp_path / "missing.json"),
                },
            ],
        }
    }
    st = compute_ai_status(cfg, {})
    assert st["ready"] is False
    assert st["missing_keys"] == ["KIMI_CODE_OAUTH"]


def test_format_ai_log_line():
    st = {"ready": True, "reason": "ok", "chain_count": 1, "missing_keys": []}
    line = format_ai_log_line(st)
    assert "AI STATUS: ON" in line
    assert "1 provider" in line
