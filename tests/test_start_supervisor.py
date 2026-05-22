from __future__ import annotations

import start


def test_normalized_args_defaults_to_paper() -> None:
    assert start._normalized_args([]) == ["--paper"]
    assert start._normalized_args(["--dashboard-only"]) == ["--dashboard-only"]


def test_child_command_runs_main_directly() -> None:
    command = start._child_command(["--paper"])

    assert command[1].endswith("src/main.py")
    assert command[-1] == "--paper"


def test_preflight_rejects_bound_dashboard_port(monkeypatch) -> None:
    monkeypatch.setattr(start, "_dashboard_bind_target", lambda: ("127.0.0.1", 8081))
    monkeypatch.setattr(start, "_port_accepts", lambda host, port: True)

    assert start._preflight_dashboard_port(["--paper"]) is False


def test_preflight_skips_one_shot_flags(monkeypatch) -> None:
    monkeypatch.setattr(start, "_dashboard_bind_target", lambda: ("127.0.0.1", 8081))
    monkeypatch.setattr(start, "_port_accepts", lambda host, port: True)

    assert start._preflight_dashboard_port(["--backtest"]) is True
