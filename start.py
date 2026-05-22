#!/usr/bin/env python3
"""
PolyBot -- Single Launch Point
==============================

  python start.py                       -> paper trading + dashboard (default)
  python start.py --paper               -> same as above (explicit)
  python start.py --dashboard-only      -> dashboard + backtests only, no trading
  python start.py --live --confirm-live -> live trading

Dashboard opens automatically in your browser at http://127.0.0.1:8081
Press Ctrl+C once in this parent process to stop the supervised child cleanly.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

# Force UTF-8 on Windows so box-drawing chars don't crash the console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent
CHILD_ENV_FLAG = "PSB_SUPERVISED_CHILD"
RUNTIME_STATUS_FILE = REPO_ROOT / "data" / "runtime" / "bot_runtime_status.json"

# Ensure project root is importable
sys.path.insert(0, str(REPO_ROOT))

ONE_SHOT_FLAGS = {"--backtest", "--emergency-stop", "--resume-trading"}
MODE_FLAGS = ("--paper", "--live", "--dashboard-only")


def _normalized_args(args: list[str]) -> list[str]:
    """Default to paper mode when no mode flag is provided."""
    out = list(args)
    if not any(flag in out for flag in MODE_FLAGS):
        out.append("--paper")
    return out


def _read_runtime_status() -> dict:
    try:
        return json.loads(RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _child_command(args: Optional[list[str]] = None) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), *_normalized_args(args or sys.argv[1:])]


def _dashboard_bind_target() -> Optional[tuple[str, int]]:
    """Return local dashboard connect target, or None when preflight should not run."""
    if os.environ.get("PORT"):
        return None
    try:
        cfg = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text()) or {}
    except Exception:
        cfg = {}
    dashboard = cfg.get("dashboard") or {}
    if dashboard.get("enabled") is False:
        return None
    host = str(dashboard.get("host") or "127.0.0.1")
    port = int(dashboard.get("dashboard_port") or 8081)
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    return connect_host, port


def _port_accepts(host: str, port: int, *, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port_release(host: str, port: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_accepts(host, port):
            return True
        time.sleep(0.25)
    return not _port_accepts(host, port)


def _preflight_dashboard_port(args: list[str]) -> bool:
    """Refuse duplicate local starts instead of spawning into an address-in-use loop."""
    if any(flag in args for flag in ONE_SHOT_FLAGS):
        return True
    target = _dashboard_bind_target()
    if target is None:
        return True
    host, port = target
    if not _port_accepts(host, port):
        return True
    print(
        f"[supervisor] dashboard port already in use at {host}:{port}; "
        "stop the existing bot first or choose a different dashboard.dashboard_port.",
        file=sys.stderr,
    )
    return False


def _run_child() -> int:
    from src.main import main

    asyncio.run(main())
    return 0


def _supervise() -> int:
    restart_delay = 5
    stop_requested = False
    args = _normalized_args(sys.argv[1:])

    while True:
        target = _dashboard_bind_target()
        if target is not None:
            host, port = target
            if not _wait_for_port_release(host, port, timeout=10.0):
                print(
                    f"[supervisor] dashboard port still in use at {host}:{port}; "
                    "not spawning another child.",
                    file=sys.stderr,
                )
                return 98
        env = dict(os.environ)
        env[CHILD_ENV_FLAG] = "1"
        started_at = time.time()
        child = subprocess.Popen(_child_command(args), env=env)
        print(f"[supervisor] started child pid={child.pid} args={' '.join(args)}")

        try:
            exit_code = child.wait()
        except KeyboardInterrupt:
            stop_requested = True
            print("[supervisor] Ctrl+C received; forwarding SIGINT to child")
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                    exit_code = child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    print("[supervisor] child did not stop after SIGINT; terminating")
                    child.terminate()
                    exit_code = child.wait(timeout=10)
            else:
                exit_code = child.returncode or 0

        status = _read_runtime_status()
        pid_matches = status.get("pid") == child.pid
        clean_shutdown = bool(status.get("clean_shutdown")) and pid_matches
        last_phase = status.get("phase", "unknown")
        runtime_sec = int(max(0, time.time() - started_at))

        if stop_requested:
            return exit_code

        if clean_shutdown:
            print(
                f"[supervisor] child exited cleanly code={exit_code} "
                f"phase={last_phase} runtime={runtime_sec}s"
            )
            return exit_code

        print(
            f"[supervisor] child exited unexpectedly code={exit_code} "
            f"phase={last_phase} runtime={runtime_sec}s; restarting in {restart_delay}s"
        )
        time.sleep(restart_delay)
        restart_delay = min(restart_delay * 2, 30)


if __name__ == "__main__":
    sys.argv = [sys.argv[0], *_normalized_args(sys.argv[1:])]
    if os.environ.get(CHILD_ENV_FLAG) == "1":
        raise SystemExit(_run_child())
    if any(flag in sys.argv[1:] for flag in ONE_SHOT_FLAGS):
        raise SystemExit(_run_child())
    if not _preflight_dashboard_port(sys.argv[1:]):
        raise SystemExit(98)
    raise SystemExit(_supervise())
