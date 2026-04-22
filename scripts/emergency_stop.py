#!/usr/bin/env python3
"""Create the kill switch file so the bot will not place new trades until resume_trading is run."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KILL_SWITCH_FILE = REPO_ROOT / "data" / "KILL_SWITCH"


def main():
    REPO_ROOT.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_FILE.touch()
    print("Kill switch enabled: data/KILL_SWITCH created.")
    print("The bot will not place new trades until you run: python scripts/resume_trading.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
