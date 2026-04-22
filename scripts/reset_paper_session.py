"""
Reset paper trade session — archive current session and start fresh.

Use when switching to a new bankroll (e.g. $500) so PnL reflects the correct
starting point. Old session data is archived for reference.

Usage:
    python scripts/reset_paper_session.py

Then restart the bot (e.g. python src/main.py --paper).
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

JOURNAL_DIR = PROJECT_ROOT / "data" / "paper_trades"
ARCHIVE_BASE = PROJECT_ROOT / "data" / "paper_trades_archive"


def main():
    if not JOURNAL_DIR.exists():
        print("No paper_trades folder found. Nothing to reset.")
        return 0

    subdirs = [d for d in JOURNAL_DIR.iterdir() if d.is_dir()]
    if not subdirs:
        print("paper_trades is empty. Nothing to reset.")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = ARCHIVE_BASE / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    for d in subdirs:
        dest = archive_dir / d.name
        shutil.move(str(d), str(dest))
        print(f"Archived: {d.name} -> {archive_dir.relative_to(PROJECT_ROOT)}/")

    print(f"\nSession archived to data/paper_trades_archive/{timestamp}/")
    print("Restart the bot to start fresh with $500 bankroll (initial_bankroll from config).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
