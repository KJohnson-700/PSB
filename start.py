#!/usr/bin/env python3
"""
PolyBot -- Single Launch Point
================================

  python start.py                       -> paper trading + dashboard (default)
  python start.py --paper               -> same as above (explicit)
  python start.py --dashboard-only      -> dashboard + backtests only, no trading
  python start.py --live --confirm-live -> live trading

Dashboard opens automatically in your browser at http://127.0.0.1:8081
Press Ctrl+C to stop.
"""
import sys
import asyncio
from pathlib import Path

# Force UTF-8 on Windows so box-drawing chars don't crash the console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent))

# Default to paper mode when no mode flag provided
_flags = sys.argv[1:]
if not any(f in _flags for f in ("--paper", "--live", "--dashboard-only")):
    sys.argv.append("--paper")

print("""
+----------------------------------------------+
|          PolyBot -- Starting Up              |
+----------------------------------------------+
|  Dashboard  ->  http://127.0.0.1:8081        |
|  Browser opens automatically when ready      |
|  Press  Ctrl+C  to stop                      |
+----------------------------------------------+
""")

from src.main import main  # noqa: E402

asyncio.run(main())
