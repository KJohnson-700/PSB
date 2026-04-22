#!/usr/bin/env python3
"""Append a backtest review note to the repo Obsidian vault (local workflow).

After you run a backtest (dashboard POST /api/backtest/start or CLI) and review output
with ``docs/polymarket-backtest-subagent-skill.md``, paste the summary here.

Example:
  python scripts/append_obsidian_backtest_review.py \\
    --title "ETH lag — Apr 2026 sweep" \\
    --body-file data/backtest/reports/bt_eth_notes.txt
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

VAULT_REL = Path("projects/polymarket-bot/strategy-log/backtest_reviews.md")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    path = root / VAULT_REL
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", required=True, help="Short review title")
    ap.add_argument("--body", default="", help="Markdown body (or use --body-file)")
    ap.add_argument("--body-file", type=Path, help="Read body from file")
    args = ap.parse_args()
    body = args.body
    if args.body_file:
        body = args.body_file.read_text(encoding="utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = f"""
## {args.title}
**Logged:** {ts}

{body.strip()}

---
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(block.strip() + "\n\n" + existing, encoding="utf-8")
    print(f"Prepended review to {path}")


if __name__ == "__main__":
    main()
