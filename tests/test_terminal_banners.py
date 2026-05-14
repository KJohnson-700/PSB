"""Unit tests for terminal startup/shutdown banner helpers."""

from __future__ import annotations

from src.terminal_banners import framed_lines, resolve_dashboard_display_url


def test_framed_lines_constant_width():
    block = framed_lines("PolyBot — starting", ["short", "x" * 80, ""], inner_width=40)
    lines = block.splitlines()
    assert len(lines) >= 4
    width = len(lines[0])
    for ln in lines:
        assert len(ln) == width


def test_resolve_dashboard_display_url_disabled():
    assert resolve_dashboard_display_url({"dashboard": {"enabled": False}}) is None


def test_resolve_dashboard_display_url_localhost():
    u = resolve_dashboard_display_url(
        {"dashboard": {"enabled": True, "host": "127.0.0.1", "dashboard_port": 8081}}
    )
    assert u == "http://127.0.0.1:8081"


def test_resolve_dashboard_display_url_zero_bind(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    u = resolve_dashboard_display_url(
        {"dashboard": {"enabled": True, "host": "0.0.0.0", "dashboard_port": 9000}}
    )
    assert u == "http://127.0.0.1:9000"


def test_resolve_dashboard_display_url_railway(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "example.up.railway.app")
    u = resolve_dashboard_display_url(
        {"dashboard": {"enabled": True, "host": "0.0.0.0", "dashboard_port": 8080}}
    )
    assert u == "https://example.up.railway.app"


def test_resolve_dashboard_display_url_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "7777")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    u = resolve_dashboard_display_url({"dashboard": {"enabled": True, "dashboard_port": 1}})
    assert u == "http://127.0.0.1:7777"
