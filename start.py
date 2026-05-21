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
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 on Windows so box-drawing chars don't crash the console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent
CHILD_ENV_FLAG = "PSB_SUPERVISED_CHILD"
RUNTIME_STATUS_FILE = REPO_ROOT / "data" / "runtime" / "bot_runtime_status.json"

# Ensure project root is importable
sys.path.insert(0, str(REPO_ROOT))

# Default to paper mode when no mode flag provided
_flags = sys.argv[1:]
if not any(f in _flags for f in ("--paper", "--live", "--dashboard-only")):
    sys.argv.append("--paper")

ONE_SHOT_FLAGS = {"--backtest", "--emergency-stop", "--resume-trading"}


def _read_runtime_status() -> dict:
    try:
        return json.loads(RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _child_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]


def _run_child() -> int:
    from src.main import main

    asyncio.run(main())
    return 0


def _supervise() -> int:
    restart_delay = 5
    stop_requested = False

    while True:
        env = dict(os.environ)
        env[CHILD_ENV_FLAG] = "1"
        started_at = time.time()
        child = subprocess.Popen(_child_command(), env=env)
        print(f"[supervisor] started child pid={child.pid} args={' '.join(sys.argv[1:])}")

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
    if os.environ.get(CHILD_ENV_FLAG) == "1":
        raise SystemExit(_run_child())
    if any(flag in sys.argv[1:] for flag in ONE_SHOT_FLAGS):
        raise SystemExit(_run_child())
    raise SystemExit(_supervise())
