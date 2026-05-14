"""Parse crypto backtest subprocess stdout for replay window progress."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Matches lines from scripts/run_backtest_crypto.py _progress, e.g.:
#   progress 1,234/5,678 windows (21.7%) trades=...
_RE_PROGRESS = re.compile(
    r"progress\s+([\d,]+)/([\d,]+)\s+windows\s+\(([\d.]+)%\)",
    re.IGNORECASE,
)


def parse_crypto_progress_from_lines(lines: List[str]) -> Dict[str, Optional[Any]]:
    """Return last progress snapshot found in ``lines`` (scan bottom-up).

    Keys: ``progress_pct`` (0–100 float), ``progress_current``, ``progress_total``.
    Values are ``None`` when no heartbeat line has been captured yet.
    """
    for line in reversed(lines or []):
        m = _RE_PROGRESS.search(line)
        if m:
            cur = int(m.group(1).replace(",", ""))
            tot = int(m.group(2).replace(",", ""))
            pct = float(m.group(3))
            return {
                "progress_pct": pct,
                "progress_current": cur,
                "progress_total": tot,
            }
    return {
        "progress_pct": None,
        "progress_current": None,
        "progress_total": None,
    }
