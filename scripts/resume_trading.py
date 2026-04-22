#!/usr/bin/env python3
"""Remove the kill switch file so the bot can place trades again."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KILL_SWITCH_FILE = REPO_ROOT / "data" / "KILL_SWITCH"


def main():
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
        print("Kill switch removed. Trading can resume.")
        return 0
    print("Kill switch file was not present. No change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
